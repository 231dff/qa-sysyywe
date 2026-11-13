from utils.semantic_cache import get_semantic_cache, _vec_to_bytes, _INDEX_NAME
from redis.commands.search.query import Query

cache = get_semantic_cache()
uid = 'u_1792055412997_bueytb2g'
target_vec = cache._embed('北京今天怎么样')
vec_bytes = _vec_to_bytes(target_vec)

# 注意这里的双引号
query_str = f"(@user_key:{{{uid}}})=>[KNN 1 @vector $vec AS knn_score]"
q = (
    Query(query_str)
    .sort_by('knn_score')
    .return_fields('query', 'answer', 'sources', 'knn_score')
    .dialect(2)
)

print('--- 正在执行 KNN 查询 ---')
try:
    result = cache._client.ft(_INDEX_NAME).search(q, query_params={'vec': vec_bytes})
    print('Docs 长度:', len(result.docs))
    if result.docs:
        doc = result.docs[0]
        print('--- doc 的原始 __dict__ ---')
        print(doc.__dict__)
        print('--- doc.knn_score 属性值 ---')
        print(repr(getattr(doc, 'knn_score', 'NO_ATTR')))
    else:
        print('❌ 查询结果为空，连 doc 都没有返回！')
except Exception as e:
    print(f'查询报错: {e}')
