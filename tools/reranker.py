"""
智能搜索助手 — 重排序器 (Reranker)
利用字面语义（BM25/TF-IDF）对切片进行相关性重排序
"""
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

def rerank_chunks(query: str, chunks: list[dict], top_k: int = 5) -> list[dict]:
    """
    对网页切片进行重排序
    chunks: [{"url": "...", "text": "..."}]
    """
    if not chunks:
        return []

    # 提取文本列表
    texts = [c["text"] for c in chunks]
    all_texts = [query] + texts

    try:
        # 简单使用字面特征进行语义相似度计算
        vectorizer = TfidfVectorizer(token_pattern=r"(?u)\b\w+\b", stop_words=None)
        tfidf_matrix = vectorizer.fit_transform(all_texts)
        
        # 计算 query (index 0) 与所有 chunks 的余弦相似度
        cosine_similarities = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:]).flatten()
        
        # 将分数附加到 chunks
        for i, chunk in enumerate(chunks):
            chunk["score"] = float(cosine_similarities[i])
            
        # 按分数降序排序，保留 Top-K
        chunks.sort(key=lambda x: x["score"], reverse=True)
        return chunks[:top_k]
    except Exception as e:
        print(f"[Reranker] 打分失败: {e}，降级为不排序")
        return chunks[:top_k]