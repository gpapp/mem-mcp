"""
test_status_monitor.py - the server status snapshot, its change fingerprint,
and the unload endpoint.

status_monitor.py is dependency-light (standard library only, nothing from
common.py and no httpx), so it is imported and *called* here rather than lifted
out of the source the way the embedding and chat suites have to. That matters
for this module specifically: the defects worth guarding are all "this looks
right and is wrong" — a cold model labelled as running on the CPU, a
fingerprint that fires on every poll, a publish that blocks on a browser that
stopped reading — and none of them is visible in the shape of the code.

The unload endpoint lives in gui.py, which cannot be imported here, so it is
lifted with ast.get_source_segment and executed against stubs.
"""

import ast
import asyncio
import inspect
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

import status_monitor as sm

HERE = os.path.dirname(os.path.abspath(__file__))


def _ps(name, size, size_vram=0, expires_in=1800, details=None):
    entry = {"name": name, "size": size, "size_vram": size_vram}
    if expires_in is not None:
        entry["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat()
    if details:
        entry["details"] = details
    return entry


class ImportDisciplineTests(unittest.TestCase):
    """The module is importable without a service, a driver or httpx.

    This is the constraint that lets the rest of the suite call the real
    functions instead of re-implementing them. It is worth pinning because
    nothing else fails when it is broken: a `from common import ...` would
    only surface as the *whole suite* erroring on import, which reads like a
    broken environment rather than a broken module.
    """

    def test_it_imports_nothing_from_the_app(self):
        source = inspect.getsource(sm)
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        allowed = {"asyncio", "json", "logging", "datetime", "typing", "__future__"}
        self.assertTrue(
            imported <= allowed,
            msg=f"status_monitor gained an import: {sorted(imported - allowed)}",
        )

    def test_it_imports_with_httpx_blocked(self):
        """Not "nothing from the app" -- httpx itself must be unnecessary.

        Checked by importing in a subprocess with an import hook that refuses
        httpx, because a docstring mentioning httpx is not an import of it and
        a substring search over the source cannot tell the two apart (it is the
        assertIn-over-a-whole-file lesson from AGENTS.md, in miniature).
        """
        script = (
            "import sys\n"
            "class Block:\n"
            "    def find_module(self, name, path=None):\n"
            "        if name == 'httpx' or name.startswith('httpx.'):\n"
            "            raise ImportError('httpx is not installed here')\n"
            "        return None\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'httpx' or name.startswith('httpx.'):\n"
            "            raise ImportError('httpx is not installed here')\n"
            "        return None\n"
            "sys.meta_path.insert(0, Block())\n"
            f"sys.path.insert(0, {HERE!r})\n"
            "import status_monitor\n"
            "snap = status_monitor.build_status(version={'version': '0.34.4'})\n"
            "assert snap['ok'], snap\n"
            "print('OK')\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("OK", result.stdout)


class ProcessorLabelTests(unittest.TestCase):
    """/api/ps reports two byte counts and no processor field.

    `ollama ps` derives the label from exactly those two numbers, so the widget
    recomputes it rather than guessing. A wrong answer here is the failure
    AGENTS.md warns about in prose: a host with the GPU overlay dropped reports
    plausible numbers and looks fine, and "100% GPU" is the only check that
    catches it.
    """

    def test_no_vram_is_cpu_inference(self):
        self.assertEqual(sm.processor_label(2_630_000_000, 0), "100% CPU")

    def test_all_vram_is_gpu(self):
        self.assertEqual(sm.processor_label(2_630_000_000, 2_630_000_000), "100% GPU")

    def test_a_split_reports_cpu_first_like_the_cli(self):
        # 3.8 GB of a model on a 4 GB card measured as 49%/51% CPU/GPU.
        label = sm.processor_label(100, 51)
        self.assertEqual(label, "49%/51% CPU/GPU")
        self.assertTrue(label.startswith("49%/"), msg="the CLI puts CPU first")

    def test_an_unknown_size_yields_no_label(self):
        self.assertEqual(sm.processor_label(0, 0), "")
        self.assertEqual(sm.processor_label(None, None), "")

    def test_junk_sizes_do_not_raise(self):
        self.assertEqual(sm.processor_label("nope", {}), "")


class SnapshotTests(unittest.TestCase):
    """The snapshot is the payload the browser draws, so its shape is the API."""

    def _snapshot(self, **kwargs):
        base = dict(
            roles=sm.model_roles(embedder="nomic-embed-text", query="nemotron-3-nano:4b",
                                 scope="nemotron-3-nano:4b", merge="gemma4:e2b",
                                 extract="granite3.3:2b"),
            url="http://ollama:11434",
            version={"version": "0.34.4"},
            tags={"models": [
                {"name": "nomic-embed-text", "size": 274_302_450},
                {"name": "nemotron-3-nano:4b", "size": 2_630_000_000},
                {"name": "granite3.3:2b", "size": 1_500_000_000},
            ]},
            ps={"models": [
                _ps("nemotron-3-nano:4b", 2_630_000_000, 2_630_000_000,
                    details={"family": "nemotron", "quantization_level": "Q4_K_M"}),
            ]},
        )
        base.update(kwargs)
        return sm.build_status(**base)

    def test_installed_and_resident_are_different_questions(self):
        snap = self._snapshot()
        by_name = {m["name"]: m for m in snap["models"]}
        self.assertTrue(by_name["nemotron-3-nano:4b"]["resident"])
        self.assertTrue(by_name["nemotron-3-nano:4b"]["installed"])
        self.assertFalse(by_name["nomic-embed-text"]["resident"])
        self.assertTrue(by_name["nomic-embed-text"]["installed"])
        self.assertEqual(snap["residentCount"], 1)
        self.assertEqual(snap["installedCount"], 3)

    def test_a_cold_model_is_not_labelled_100_percent_cpu(self):
        """The byte count of an installed model is not a running model.

        Deriving the label for every listed model paints every cold model
        "100% CPU", which reads on screen as a model running on the processor
        and hides the one thing the widget exists to show.
        """
        snap = self._snapshot()
        by_name = {m["name"]: m for m in snap["models"]}
        self.assertEqual(by_name["nomic-embed-text"]["processor"], "")
        self.assertEqual(by_name["nemotron-3-nano:4b"]["processor"], "100% GPU")

    def test_a_resident_model_carries_its_keep_alive(self):
        snap = self._snapshot()
        resident = next(m for m in snap["models"] if m["resident"])
        self.assertTrue(resident["expiresAt"], msg="the countdown has nothing to count down from")
        self.assertTrue(resident["family"] and resident["quantization"])

    def test_shared_models_keep_every_role(self):
        """Two roles on one model is the documented default, not a duplicate.

        Collapsing them per model would drop `scope` from the row an operator
        is reading when deciding what to evict.
        """
        snap = self._snapshot()
        nemotron = next(m for m in snap["models"] if m["name"] == "nemotron-3-nano:4b")
        self.assertEqual(sorted(nemotron["roles"]), ["query", "scope"])
        self.assertEqual(len(sm.model_roles("e", "q", "x", "q2", "m")), 5)

    def test_resident_models_sort_first(self):
        """A widget listing a cold model above a resident one reads as broken."""
        names = [m["name"] for m in self._snapshot()["models"]]
        self.assertEqual(names[0], "nemotron-3-nano:4b")
        self.assertTrue(all(m["resident"] for m in self._snapshot()["models"][:1]))

    def test_configured_models_appear_even_when_ollama_has_not_heard_of_them(self):
        """A missing model is the state an operator most needs to see."""
        snap = self._snapshot(
            tags={"models": []}, ps={"models": []},
            roles=sm.model_roles(embedder="nomic-embed-text", merge="gemma4:e2b"),
        )
        by_name = {m["name"]: m for m in snap["models"]}
        self.assertFalse(by_name["gemma4:e2b"]["installed"])
        self.assertTrue(by_name["gemma4:e2b"]["configured"])
        self.assertFalse(by_name["gemma4:e2b"]["resident"])

    def test_a_dead_service_is_a_snapshot_not_an_exception(self):
        """The configured models still list, so the widget can say which are missing."""
        snap = self._snapshot(version=None, tags=None, ps=None,
                              error="ConnectError: connection refused")
        self.assertFalse(snap["ok"])
        self.assertIn("connection refused", snap["error"])
        self.assertEqual(snap["residentCount"], 0)
        self.assertTrue(all(not m["installed"] for m in snap["models"]))

    def test_a_broken_endpoint_degrades_instead_of_painting_the_service_down(self):
        """`ok` is the whole service; `warnings` is one route.

        Collapsing the two either hides a moved route behind a green light or
        declares the service dead because one endpoint changed shape.
        """
        snap = self._snapshot(warnings=["/api/tags returned HTTP 404: not found"])
        self.assertTrue(snap["ok"])
        self.assertEqual(len(snap["warnings"]), 1)
        self.assertTrue(any(m["resident"] for m in snap["models"]))

    def test_the_maintenance_lock_travels_with_the_snapshot(self):
        """A vault that is up but not answering is explained by the lock."""
        snap = self._snapshot(maintenance={"test-user": "reclassify"})
        self.assertEqual(snap["maintenance"], {"test-user": "reclassify"})

    def test_ollama_answers_a_model_key_as_well_as_a_name_key(self):
        """Both spellings have shipped; looking for only one drops every model."""
        snap = self._snapshot(
            tags={"models": [{"model": "granite3.3:2b", "size": 10}]},
            ps={"models": [{"model": "granite3.3:2b", "size": 10, "size_vram": 10}]},
        )
        granite = next(m for m in snap["models"] if m["name"] == "granite3.3:2b")
        self.assertTrue(granite["resident"])
        self.assertEqual(granite["processor"], "100% GPU")


class SignatureTests(unittest.TestCase):
    """The fingerprint is what decides whether the SSE stream says anything.

    If it is taken over the wrong fields the widget is either frozen (a
    fingerprint that misses real changes) or rewritten sixty times a minute (one
    that includes the wall clock), and neither failure raises anything.
    """

    def _snapshot(self, **kwargs):
        base = dict(
            roles=sm.model_roles(query="nemotron-3-nano:4b"),
            url="http://ollama:11434",
            version={"version": "0.34.4"},
            ps={"models": [_ps("nemotron-3-nano:4b", 2_630_000_000, 2_630_000_000)]},
        )
        base.update(kwargs)
        return sm.build_status(**base)

    def test_the_wall_clock_is_not_state(self):
        """`checked` moves between two identical observations. By design."""
        first = self._snapshot()
        second = self._snapshot()
        self.assertNotEqual(first["checked"], second["checked"])
        self.assertEqual(sm.signature(first), sm.signature(second))

    def test_the_documented_volatile_keys_are_the_ones_excluded(self):
        """Kept in step with signature(), and asserted rather than trusted.

        `expiresAt` is a per-model key and `checked` a top-level one, so each
        is asserted where it actually lives -- a top-level-only check would
        have reported the countdown as "absent" and passed for the wrong reason.
        """
        snap = self._snapshot()
        model = snap["models"][0]
        for key in sm.VOLATILE_SNAPSHOT_KEYS:
            container = model if key in model else snap
            self.assertIn(key, container, msg=f"{key} is documented as volatile but absent")
            moved = dict(container)
            moved[key] = "1999-01-01T00:00:00+00:00"
            self.assertEqual(
                sm.signature(snap), sm.signature(snap),
                msg="baseline drifted",
            )
            if container is model:
                # A per-model change: rebuild the snapshot with the value moved.
                altered = dict(snap, models=[dict(model, **{key: moved[key]})])
            else:
                altered = dict(snap, **{key: moved[key]})
            self.assertEqual(
                sm.signature(snap), sm.signature(altered),
                msg=f"a change to {key} must not read as a state change",
            )

    def test_a_model_loading_is_a_change(self):
        cold = self._snapshot(ps={"models": []})
        warm = self._snapshot()
        self.assertNotEqual(sm.signature(cold), sm.signature(warm))

    def test_a_model_unloading_is_a_change(self):
        warm = self._snapshot()
        cold = self._snapshot(ps={"models": []})
        self.assertNotEqual(sm.signature(warm), sm.signature(cold))

    def test_a_partial_eviction_shows_up(self):
        """49%/51% one way and 51%/49% the other are different states."""
        half = self._snapshot(ps={"models": [_ps("nemotron-3-nano:4b", 100, 49)]})
        other = self._snapshot(ps={"models": [_ps("nemotron-3-nano:4b", 100, 51)]})
        self.assertNotEqual(sm.signature(half), sm.signature(other))

    def test_going_down_and_coming_back_are_changes(self):
        alive = self._snapshot()
        dead = self._snapshot(version=None, ps=None, error="ConnectError")
        self.assertNotEqual(sm.signature(alive), sm.signature(dead))
        self.assertNotEqual(sm.signature(dead), sm.signature(alive))

    def test_the_version_a_warning_and_the_lock_are_state(self):
        for changed in (
            self._snapshot(version={"version": "0.35.0"}),
            self._snapshot(warnings=["/api/tags returned HTTP 500"]),
            self._snapshot(maintenance={"test-user": "reclassify"}),
        ):
            self.assertNotEqual(sm.signature(self._snapshot()), sm.signature(changed))

    def test_the_key_order_of_the_payload_does_not_matter(self):
        """Two observations of the same state fingerprint the same, however built."""
        roles = sm.model_roles(query="nemotron-3-nano:4b")
        forward = sm.build_status(
            roles=roles, url="u", version={"version": "0.34.4"},
            ps={"models": [_ps("nemotron-3-nano:4b", 100, 100)]},
        )
        backward = sm.build_status(
            ps={"models": [_ps("nemotron-3-nano:4b", 100, 100)]},
            version={"version": "0.34.4"}, url="u", roles=roles,
        )
        self.assertEqual(sm.signature(forward), sm.signature(backward))


class PublishTests(unittest.TestCase):
    """One poller, N browsers, and no queue that can wedge the poller."""

    def setUp(self):
        sm.reset()

    def tearDown(self):
        sm.reset()

    def _snapshot(self, resident=True):
        return sm.build_status(
            roles=sm.model_roles(query="nemotron-3-nano:4b"),
            version={"version": "0.34.4"},
            ps={"models": [_ps("nemotron-3-nano:4b", 100, 100)]} if resident else {"models": []},
        )

    def test_the_first_snapshot_is_published_and_the_same_one_is_not(self):
        queue = sm.subscribe()
        self.assertTrue(asyncio.run(sm.publish(self._snapshot())))
        self.assertFalse(asyncio.run(sm.publish(self._snapshot())),
                         msg="an unchanged snapshot must not wake every browser")
        self.assertEqual(queue.qsize(), 1)

    def test_a_change_reaches_every_subscriber(self):
        queues = [sm.subscribe() for _ in range(3)]
        asyncio.run(sm.publish(self._snapshot()))
        asyncio.run(sm.publish(self._snapshot(resident=False)))
        for queue in queues:
            self.assertEqual(queue.qsize(), 1)
            self.assertEqual(queue.get_nowait()["residentCount"], 0)

    def test_a_slow_browser_drops_the_old_snapshot_instead_of_blocking(self):
        """Bounded queue, oldest evicted: the next poll replaces it anyway."""
        queue = sm.subscribe()
        self.assertEqual(queue.maxsize, 1)
        asyncio.run(sm.publish(self._snapshot()))
        asyncio.run(sm.publish(self._snapshot(resident=False)))
        self.assertEqual(queue.qsize(), 1, msg="the poller blocked on an unread queue")
        self.assertEqual(queue.get_nowait()["residentCount"], 0,
                         msg="the retained snapshot is the stale one")

    def test_unsubscribe_stops_the_pushes(self):
        queue = sm.subscribe()
        self.assertEqual(sm.subscriber_count(), 1)
        sm.unsubscribe(queue)
        asyncio.run(sm.publish(self._snapshot()))
        self.assertEqual(queue.qsize(), 0)
        self.assertEqual(sm.subscriber_count(), 0)

    def test_unsubscribing_twice_is_harmless(self):
        """A closed stream runs its finally block once per disconnect path."""
        queue = sm.subscribe()
        sm.unsubscribe(queue)
        sm.unsubscribe(queue)
        self.assertEqual(sm.subscriber_count(), 0)

    def test_last_snapshot_is_what_a_new_connection_is_served(self):
        self.assertIsNone(sm.last_snapshot())
        snapshot = self._snapshot()
        asyncio.run(sm.publish(snapshot))
        self.assertEqual(sm.last_snapshot(), snapshot)


class BroadcastLoopTests(unittest.TestCase):
    """The loop is the only thing that polls, so its cadence is the contract."""

    def setUp(self):
        sm.reset()

    def tearDown(self):
        sm.reset()

    def test_it_probes_immediately_and_stops_when_cancelled(self):
        calls = []

        async def probe():
            calls.append(len(calls))
            return sm.build_status(version={"version": "0.34.4"})

        async def run():
            task = asyncio.create_task(sm.broadcast_loop(probe, 3600))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        self.assertEqual(len(calls), 1,
                         msg="a page loading just after startup waits a whole interval for a snapshot")

    def test_a_failing_probe_is_logged_and_does_not_kill_the_loop(self):
        """One unreachable Ollama must not end status updates for the process.

        Asserted with `assertLogs` because a swallowed failure is the other
        half of the same defect: the loop survives either way, so without the
        log the test would pass on a version that said nothing about why the
        widget has been stale since the last restart.
        """
        calls = []

        async def probe():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("ollama is restarting")
            return sm.build_status(version={"version": "0.34.4"})

        async def run():
            task = asyncio.create_task(sm.broadcast_loop(probe, 1.0))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with self.assertLogs("memory-vault.status", level="WARNING") as captured:
            asyncio.run(run())
        self.assertTrue(any("ollama is restarting" in line for line in captured.output),
                        msg="the probe failure was swallowed silently")
        self.assertEqual(len(calls), 1, msg="the loop stopped after one failure")

    def test_the_interval_has_a_floor(self):
        """`0` would otherwise spin the loop against Ollama."""
        seen = []

        async def probe():
            seen.append(1)
            return sm.build_status(version={"version": "0.34.4"})

        async def run():
            task = asyncio.create_task(sm.broadcast_loop(probe, 0))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        self.assertEqual(len(seen), 1, msg="interval 0 polled in a tight loop")


class UnloadEndpointTests(unittest.TestCase):
    """api_unload_model, run against a stub Ollama.

    Unloading a model Ollama has not loaded answers 200 and does nothing, so
    the failure mode of a typo is an operation that reports success and changes
    nothing. That is only guarded by validating the name against the live
    snapshot, which no source-shape assertion can see.
    """

    class _HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    def _runner(self, after=None, fail=False):
        """Exec the lifted endpoint; returns (endpoint, unloaded_models).

        Only the HTTP is stubbed. `_status_snapshot` is re-implemented here
        with the same two lines the module uses, because the real one is a
        module-level function the lifted body calls by name -- stubbing it away
        wholesale would skip the very branch (no snapshot yet) worth seeing.
        """
        with open(os.path.join(HERE, "gui.py"), encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_unload_model"
        )
        unloaded = []
        after = after if after is not None else sm.build_status(
            version={"version": "0.34.4"}, ps={"models": []})

        class _Mem:
            async def unload_ollama_model(self, model):
                unloaded.append(model)
                if fail:
                    raise RuntimeError(f"Ollama refused to unload {model} (HTTP 500)")

            async def fetch_ollama_status(self):
                return after

        mem = _Mem()

        async def _status_snapshot():
            snapshot = sm.last_snapshot()
            if snapshot is None:
                await sm.publish(await mem.fetch_ollama_status())
                snapshot = sm.last_snapshot()
            return snapshot

        namespace = {
            "HTTPException": self._HTTPException,
            "JSONResponse": object,
            "ModelUnload": object,
            "Request": object,
            "Response": object,
            "asyncio": asyncio,
            "json": __import__("json"),
            "logging": __import__("logging"),
            "mem": mem,
            "status_monitor": sm,
            "_status_snapshot": _status_snapshot,
            "_require_user": lambda request: "test-user",
            "_service_unavailable": lambda exc: self._HTTPException(503, str(exc)),
        }
        func.decorator_list = []
        exec(compile(ast.Module(body=[func], type_ignores=[]), "gui.py", "exec"), namespace)
        sm.reset()
        return namespace["api_unload_model"], unloaded

    def _body(self, model):
        return type("B", (), {"model": model})()

    @staticmethod
    def _warm(**kwargs):
        base = dict(
            roles=sm.model_roles(query="nemotron-3-nano:4b"),
            version={"version": "0.34.4"},
            ps={"models": [_ps("nemotron-3-nano:4b", 100, 100)]},
        )
        base.update(kwargs)
        return sm.build_status(**base)

    def test_a_live_model_is_unloaded_and_the_change_is_broadcast(self):
        after = self._warm(ps={"models": []})
        endpoint, unloaded = self._runner(after=after)

        async def run():
            sm.reset()
            await sm.publish(self._warm())
            queue = sm.subscribe()
            return await endpoint(object(), self._body("nemotron-3-nano:4b")), queue

        (result, queue) = asyncio.run(run())
        self.assertEqual(unloaded, ["nemotron-3-nano:4b"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"], msg="an unload that changed nothing reported no change")
        self.assertEqual(result["status"]["residentCount"], 0)
        self.assertEqual(queue.qsize(), 1, msg="nobody watching the stream was told")
        self.assertEqual(queue.get_nowait()["residentCount"], 0)

    def test_an_unknown_model_is_refused_before_the_request(self):
        endpoint, unloaded = self._runner()

        async def run():
            sm.reset()
            await sm.publish(self._warm())
            return await endpoint(object(), self._body("nemotron-3-nano:4"))

        with self.assertRaises(self._HTTPException) as caught:
            asyncio.run(run())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(unloaded, [], msg="the request went out anyway")
        self.assertIn("nemotron-3-nano:4b", caught.exception.detail,
                      msg="the refusal must name the models that are actually loaded")

    def test_an_empty_model_name_is_a_400(self):
        endpoint, unloaded = self._runner()

        async def run():
            sm.reset()
            await sm.publish(self._warm())
            return await endpoint(object(), self._body("   "))

        with self.assertRaises(self._HTTPException) as caught:
            asyncio.run(run())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(unloaded, [])

    def test_an_ollama_failure_becomes_a_503_that_says_why(self):
        """The same argument as every other RuntimeError handler in gui.py."""
        endpoint, _ = self._runner(fail=True)

        async def run():
            sm.reset()
            await sm.publish(self._warm())
            return await endpoint(object(), self._body("nemotron-3-nano:4b"))

        with self.assertRaises(self._HTTPException) as caught:
            asyncio.run(run())
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("refused to unload", caught.exception.detail)

    def test_a_snapshot_never_taken_is_filled_in_first(self):
        """The startup race: no poller output yet, and the stream still answers."""
        endpoint, unloaded = self._runner(after=self._warm())

        async def run():
            sm.reset()
            return await endpoint(object(), self._body("nemotron-3-nano:4b"))

        result = asyncio.run(run())
        self.assertEqual(unloaded, ["nemotron-3-nano:4b"])
        self.assertIsNotNone(sm.last_snapshot())


class StatusEndpointTests(unittest.TestCase):
    """_status_snapshot / api_status, run — because a bool is not a snapshot.

    `publish()` answers *whether anything changed*, not what the snapshot is.
    Assigning its return value where a snapshot is wanted hands the caller
    `True`, and the symptom is an `AttributeError` on `.get()` two lines later
    in a different endpoint. The line reads correctly, so only running it
    catches this.
    """

    def _runner(self):
        with open(os.path.join(HERE, "gui.py"), encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        wanted = {"_status_snapshot", "api_status"}
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name in wanted]
        self.assertEqual({f.name for f in funcs}, wanted, msg="a helper moved or was renamed")
        probes = []

        class _Mem:
            async def fetch_ollama_status(self):
                probes.append(1)
                return sm.build_status(version={"version": "0.34.4"})

        namespace = {
            "JSONResponse": object, "Request": object, "mem": _Mem(),
            "status_monitor": sm, "_require_user": lambda request: "test-user",
        }
        for func in funcs:
            func.decorator_list = []
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "gui.py", "exec"), namespace)
        sm.reset()
        return namespace["_status_snapshot"], namespace["api_status"], probes

    def test_it_returns_a_snapshot_dict_on_the_cold_path(self):
        helper, api_status, probes = self._runner()

        async def run():
            return await api_status(object())

        result = asyncio.run(run())
        self.assertIsInstance(result, dict, msg=f"got {type(result).__name__}, not a snapshot")
        self.assertTrue(result["ok"])
        self.assertEqual(len(probes), 1, msg="the cold path did not probe Ollama")

    def test_a_warm_snapshot_is_served_without_probing(self):
        helper, api_status, probes = self._runner()
        snapshot = sm.build_status(version={"version": "9.9.9"})

        async def run():
            await sm.publish(snapshot)
            return await api_status(object())

        result = asyncio.run(run())
        self.assertEqual(result["version"], "9.9.9")
        self.assertEqual(probes, [], msg="a second browser tab re-probed Ollama")


class StatusProbeTests(unittest.TestCase):
    """fetch_ollama_status / unload_ollama_model, run against a fake httpx.

    common.py cannot be imported here (httpx, the drivers, FastAPI), so the
    two functions are lifted with ast.get_source_segment and executed against a
    client stub. They are the only place that knows the *shape* of Ollama's
    answers, and both of the interesting decisions live there: a failed route
    is a warning rather than a dead service, and a dead service is a snapshot
    rather than an exception.
    """

    class _Response:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.text = text or ("" if payload is None else str(payload))

        def json(self):
            if self._payload is None:
                raise ValueError("no JSON body")
            return self._payload

    def _runner(self, responses, raise_on=()):
        """Lift both functions; `responses` maps endpoint -> _Response/Exception."""
        with open(os.path.join(HERE, "common.py"), encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        wanted = {"fetch_ollama_status", "unload_ollama_model"}
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name in wanted]
        self.assertEqual({f.name for f in funcs}, wanted, msg="a helper moved or was renamed")
        posted = []
        _Response = self._Response

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, **kw):
                for endpoint, answer in responses.items():
                    if url.endswith(endpoint):
                        if isinstance(answer, Exception) and endpoint in raise_on:
                            raise answer
                        return answer
                return _Response(404, text="not found")

            async def post(self, url, json=None, **kw):
                posted.append({"url": url, "body": json})
                answer = responses.get("/api/generate")
                if isinstance(answer, Exception):
                    raise answer
                return answer or _Response(200, {"done": True})

        logs = []
        namespace = {
            "asyncio": asyncio, "json": __import__("json"),
            "httpx": type("H", (), {"AsyncClient": _Client}),
            "status_monitor": sm,
            "OLLAMA_URL": "http://ollama:11434",
            "STATUS_HTTP_TIMEOUT": 8.0,
            "logger": type("L", (), {"warning": staticmethod(lambda *a: logs.append(a))})(),
            "active_maintenance": lambda: {"test-user": "reclassify"},
            "configured_model_roles": lambda: sm.model_roles(query="nemotron-3-nano:4b"),
            "_ollama_detail": lambda resp: resp.text or "no detail",
        }
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "common.py", "exec"), namespace)
        return namespace["fetch_ollama_status"], namespace["unload_ollama_model"], posted, logs

    def _healthy(self):
        return {
            "/api/version": self._Response(200, {"version": "0.34.4"}),
            "/api/ps": self._Response(200, {"models": [
                {"name": "nemotron-3-nano:4b", "size": 100, "size_vram": 100}]}),
            "/api/tags": self._Response(200, {"models": [
                {"name": "nemotron-3-nano:4b", "size": 100}]}),
        }

    def test_a_healthy_service_produces_a_live_snapshot(self):
        probe, _, _, _ = self._runner(self._healthy())
        snap = asyncio.run(probe())
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["version"], "0.34.4")
        self.assertEqual(snap["residentCount"], 1)
        self.assertEqual(snap["maintenance"], {"test-user": "reclassify"})
        self.assertTrue(snap["checked"], msg="the settings page shows how old this is")

    def test_one_moved_route_degrades_the_snapshot_rather_than_emptying_it(self):
        responses = self._healthy()
        responses["/api/tags"] = self._Response(404, text="not found")
        probe, _, _, _ = self._runner(responses)
        snap = asyncio.run(probe())
        self.assertTrue(snap["ok"], msg="a moved route painted the whole service down")
        self.assertEqual(len(snap["warnings"]), 1)
        self.assertIn("/api/tags", snap["warnings"][0])
        self.assertIn("404", snap["warnings"][0])
        self.assertEqual(snap["residentCount"], 1, msg="the part that did answer was discarded")

    def test_an_unparseable_body_is_a_warning_too(self):
        responses = self._healthy()
        responses["/api/ps"] = self._Response(200, text="<html>login</html>")
        probe, _, _, _ = self._runner(responses)
        snap = asyncio.run(probe())
        self.assertTrue(snap["ok"])
        self.assertTrue(any("not JSON" in w for w in snap["warnings"]))
        self.assertEqual(snap["residentCount"], 0)

    def test_a_dead_service_is_a_snapshot_not_an_exception(self):
        """The widget has to be able to *show* the service being down."""
        responses = {ep: ConnectionError("connection refused") for ep in
                     ("/api/version", "/api/ps", "/api/tags")}
        probe, _, _, _ = self._runner(responses, raise_on=tuple(responses))
        snap = asyncio.run(probe())
        self.assertFalse(snap["ok"])
        self.assertIn("connection refused", snap["error"])
        self.assertEqual(snap["residentCount"], 0)

    def test_unload_sends_keep_alive_zero(self):
        """The only supported way to evict. A positive value extends, not frees."""
        _, unload, posted, _ = self._runner(self._healthy())
        asyncio.run(unload("nemotron-3-nano:4b"))
        self.assertEqual(len(posted), 1, msg="no unload request was issued")
        self.assertTrue(posted[0]["url"].endswith("/api/generate"))
        self.assertEqual(posted[0]["body"], {"model": "nemotron-3-nano:4b", "keep_alive": 0})

    def test_a_refused_unload_raises_with_the_reason(self):
        """Not an HTTPStatusError: nothing upstream catches httpx's."""
        responses = self._healthy()
        responses["/api/generate"] = self._Response(500, text="no such model")
        _, unload, _, _ = self._runner(responses)
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(unload("nemotron-3-nano:4b"))
        self.assertIn("no such model", str(caught.exception))
        self.assertIn("nemotron-3-nano:4b", str(caught.exception))

    def test_an_unreachable_ollama_raises_on_unload_rather_than_succeeding(self):
        """The other half of the silent no-op: a 200 that never arrived."""
        responses = self._healthy()
        responses["/api/generate"] = ConnectionError("connection refused")
        _, unload, _, _ = self._runner(responses)
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(unload("nemotron-3-nano:4b"))
        self.assertIn("connection refused", str(caught.exception))


class RouteWiringTests(unittest.TestCase):
    """The three routes exist, and the poller is started by the lifespan.

    Source-shaped, and deliberately secondary to the suites above: it pins that
    the browser has somewhere to connect, which no behavioural test of the
    snapshot itself can see.
    """

    def setUp(self):
        with open(os.path.join(HERE, "gui.py"), encoding="utf-8") as handle:
            self.gui = handle.read()
        with open(os.path.join(HERE, "server.py"), encoding="utf-8") as handle:
            self.server = handle.read()

    def test_the_three_routes_are_registered(self):
        for route in ('@web_app.get("/api/status", response_class=JSONResponse)',
                      '@web_app.get("/api/status/stream")',
                      '@web_app.post("/api/status/models/unload", response_class=JSONResponse)'):
            self.assertIn(route, self.gui, msg=f"{route} is missing")

    def test_the_stream_sends_a_heartbeat(self):
        """An idle stream is the normal case; a proxy closes it without one."""
        self.assertIn('"event": "ping"', self.gui)

    def test_the_poller_is_cancelled_with_the_other_lifespan_tasks(self):
        """The poller must not outlive the app on shutdown.

        Asserted on status_task being a *member* of the cancelled tuple rather
        than on the tuple's exact text. The literal form was correct about the
        poller and wrong about everything else: adding a fourth lifespan task
        (the session GC) broke a test whose only subject was the poller. This is
        the "an assertion that counts occurrences is asserting on the dead code
        too" lesson — pin the property, not the spelling.
        """
        self.assertIn("status_monitor.broadcast_loop", self.server)
        cancel = [line for line in self.server.splitlines()
                  if "for task in (" in line]
        self.assertEqual(len(cancel), 1, f"expected one cancellation loop, got {cancel}")
        self.assertIn("status_task", cancel[0])


if __name__ == "__main__":
    unittest.main()
