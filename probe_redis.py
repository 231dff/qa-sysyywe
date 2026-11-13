import redis
from config import get_agent_config

cfg = get_agent_config()
client = redis.Redis(
    host=cfg.redis_host, port=cfg.redis_port, db=cfg.redis_db,
    password=cfg.redis_password or None, decode_responses=True
)

print("=== 1. 当前所有索引列表 ===")
try:
    print(client.execute_command("FT._LIST"))
except Exception as e:
    print("查询失败:", e)

print("\n=== 2. semantic_cache_idx 的详细信息 ===")
try:
    info = client.execute_command("FT.INFO", "semantic_cache_idx")
    # 将 list 转为 dict 方便看
    info_dict = dict(zip(info[::2], info[1::2]))
    print(f"num_docs (已索引数据量): {info_dict.get('num_docs')}")
    print(f"indexing (是否正在建索引): {info_dict.get('indexing')}")
    print(f"percent_indexed: {info_dict.get('percent_indexed')}")
except Exception as e:
    print("查询失败:", e)

print("\n=== 3. 暴力 KNN 查询（不加 user_key 过滤） ===")
import struct
from utils.semantic_cache import _vec_to_bytes, get_semantic_cache

cache = get_semantic_cache()
target_vec = cache._embed('你好')
vec_bytes = _vec_to_bytes(target_vec)

query = "(*)=>[KNN 3 @vector $vec AS knn_score]"
try:
    res = client.execute_command(
        "FT.SEARCH", "semantic_cache_idx", query,
        "PARAMS", 2, "vec", vec_bytes,
        "DIALECT", 2
    )
    print("返回结果总数:", res[0])
    if res[0] > 0:
        print("可以查到数据！第一条是:", res[1])
except Exception as e:
    print("查询失败:", e)
