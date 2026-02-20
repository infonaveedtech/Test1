import asyncio
import time
import aiohttp

URL = "http://192.168.36.83:8000/v1/pipeline/run"

# what user asks
QUERY_TEXT = "Show total revenue by month for the last 12 months"

# concurrency knobs
CONCURRENT_USERS = 16   # change later: 1,2,4,8,16
TIMEOUT = 600

# pipeline options (match backend's query params)
PARAMS = {
    "query": QUERY_TEXT,
    "run_db": "false",
    "row_limit": "100",
    "host": "",          # keep empty unless you override ollama host per request
    "model": "",         # keep empty unless you override model per request
    "oracle_owner": "",
    "schema_card_path": "",
    "max_schema_tables": "10",
}

async def call_api(session, idx):
    start = time.time()
    try:
        async with session.post(URL, params=PARAMS, timeout=TIMEOUT) as resp:
            _ = await resp.text()
            return resp.status, time.time() - start
    except Exception as e:
        return f"ERR:{type(e).__name__}", time.time() - start

async def main():
    async with aiohttp.ClientSession() as session:
        tasks = [call_api(session, i) for i in range(CONCURRENT_USERS)]
        results = await asyncio.gather(*tasks)

    times = [t for _, t in results]
    ok = sum(1 for s, _ in results if s == 200)
    fail = len(results) - ok

    print("\n=== RESULT ===")
    print("Concurrent users:", CONCURRENT_USERS)
    print("Statuses:", [s for s, _ in results])
    print("Success:", ok, "Fail:", fail)
    print("Min time:", round(min(times), 2), "sec")
    print("Avg time:", round(sum(times)/len(times), 2), "sec")
    print("Max time:", round(max(times), 2), "sec")

if __name__ == "__main__":
    asyncio.run(main())


# import asyncio
# import time
# import aiohttp
# import statistics

# URL = "http://192.168.36.83:8000/v1/pipeline/run"

# QUERY_TEXT = "Show total revenue by month for the last 12 months"

# PARAMS = {
#     "query": QUERY_TEXT,
#     "run_db": "false",
#     "row_limit": "100",
# }

# TOTAL_USERS = 8
# RAMP_SECONDS = 60          # ramp up over 60s instead of instant
# TIMEOUT = 1900              # 15 min timeout (because queues happen)

# async def one_user(session, user_id, start_delay):
#     await asyncio.sleep(start_delay)
#     t0 = time.time()
#     try:
#         async with session.post(URL, params=PARAMS, timeout=TIMEOUT) as resp:
#             await resp.text()
#             return resp.status, time.time() - t0
#     except Exception as e:
#         return "ERR", time.time() - t0

# async def main():
#     connector = aiohttp.TCPConnector(limit=0)
#     async with aiohttp.ClientSession(connector=connector) as session:
#         tasks = []
#         for i in range(TOTAL_USERS):
#             delay = (i / TOTAL_USERS) * RAMP_SECONDS
#             tasks.append(one_user(session, i, delay))

#         t_start = time.time()
#         results = await asyncio.gather(*tasks)
#         t_total = time.time() - t_start

#     statuses = [s for s, _ in results]
#     times = [t for _, t in results]

#     ok = sum(1 for s in statuses if s == 200)
#     fail = len(statuses) - ok

#     print("\n=== 500 USER TEST RESULTS ===")
#     print("Total users:", TOTAL_USERS)
#     print("Ramp seconds:", RAMP_SECONDS)
#     print("Total wall time:", round(t_total, 2), "sec")
#     print("OK:", ok, "FAIL:", fail)

#     if ok:
#         ok_times = [t for s, t in results if s == 200]
#         print("Latency (sec) min/avg/p50/p95/max:",
#               round(min(ok_times),2),
#               round(sum(ok_times)/len(ok_times),2),
#               round(statistics.median(ok_times),2),
#               round(sorted(ok_times)[int(0.95*len(ok_times))-1],2),
#               round(max(ok_times),2))

#         # throughput: completed requests per minute
#         rpm = ok / (t_total / 60)
#         print("Throughput:", round(rpm, 2), "requests/minute")

# if __name__ == "__main__":
#     asyncio.run(main())
