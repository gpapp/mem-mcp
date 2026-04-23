from qdrant_client import AsyncQdrantClient
import asyncio

async def main():
    client = AsyncQdrantClient(url="http://localhost:6333")
    print(f"Has search: {hasattr(client, 'search')}")
    print(f"Has query_points: {hasattr(client, 'query_points')}")
    print(f"Methods: {[m for m in dir(client) if not m.startswith('_')]}")

if __name__ == "__main__":
    asyncio.run(main())
