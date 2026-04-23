from mcp_tools import mcp
import asyncio

async def test():
    print("Testing mcp.http_app...")
    try:
        app = mcp.http_app
        print("Success! App created.")
        for route in app.routes:
            print(f"Route: {route.path}")
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test())
