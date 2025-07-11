import os
import numpy as np
import faiss
from typing import List, Dict, Any
import logging
from django.conf import settings
import pickle
from pathlib import Path
import gc  # 添加垃圾回收模块

from ..models import Document, DocumentChunk
from .embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

class VectorDBService:
    """向量数据库服务，用于存储和检索文档向量"""
    
    def __init__(self):
        self.embedding_service = EmbeddingService()
        self.vector_store_path = settings.VECTOR_STORE_PATH
        self.index_file = os.path.join(self.vector_store_path, "faiss_index.bin")
        self.mapping_file = os.path.join(self.vector_store_path, "chunk_mapping.pkl")
        self.vector_dim = 1024  # 向量维度
        
        # 确保向量库目录存在
        Path(self.vector_store_path).mkdir(parents=True, exist_ok=True)
        
        # 初始化或加载索引
        self._init_index()
    
    def _init_index(self) -> None:
        """初始化FAISS索引"""
        if os.path.exists(self.index_file) and os.path.exists(self.mapping_file):
            # 加载现有索引
            try:
                self.index = faiss.read_index(self.index_file)
                with open(self.mapping_file, 'rb') as f:
                    self.chunk_mapping = pickle.load(f)
                logger.info(f"已加载现有FAISS索引，包含{self.index.ntotal}个向量")
            except Exception as e:
                logger.error(f"加载索引失败: {str(e)}，将创建新索引")
                self._create_new_index()
        else:
            # 创建新索引
            self._create_new_index()
    
    def _create_new_index(self) -> None:
        """创建新的FAISS索引"""
        # 使用内积索引，效率更高，且内存占用更小
        # 对于更大规模应用，可以考虑使用HNSW或IVF索引
        self.index = faiss.IndexFlatIP(self.vector_dim)  
        self.chunk_mapping = {}  # 存储向量ID到文档块ID的映射
        logger.info("已创建新的FAISS索引")
    
    def _save_index(self) -> None:
        """保存索引到文件"""
        try:
            faiss.write_index(self.index, self.index_file)
            with open(self.mapping_file, 'wb') as f:
                pickle.dump(self.chunk_mapping, f)
            logger.info(f"FAISS索引已保存，包含{self.index.ntotal}个向量")
        except Exception as e:
            logger.error(f"保存索引失败: {str(e)}")
    
    def index_document(self, document: Document) -> bool:
        """将文档索引到向量数据库"""
        try:
            # 检查文档状态
            if document.status == 'failed':
                logger.warning(f"文档{document.id}状态为failed，跳过索引")
                return False
                
            # 获取文档分块
            chunks = DocumentChunk.objects.filter(document_id=document.id)
            
            if not chunks:
                logger.warning(f"文档{document.id}没有分块，无法索引")
                return False
            
            # 获取分块文本向量，分批处理以减少内存使用
            start_idx = self.index.ntotal
            batch_size = 10  # 每批处理的块数量，调整此值可以平衡内存使用和处理效率
            total_vectors = 0
            
            # 分批处理文档块
            for i in range(0, chunks.count(), batch_size):
                # 获取当前批次的文档块
                batch_chunks = chunks[i:i + batch_size]
                vectors = []
                chunk_ids = []
                
                # 处理每个文档块
                for chunk in batch_chunks:
                    try:
                        # 获取文本向量嵌入
                        vector = self.embedding_service.get_embedding(chunk.content)
                        vectors.append(vector)
                        chunk_ids.append(chunk.id)
                    except Exception as e:
                        logger.error(f"处理文档块{chunk.id}时出错: {str(e)}")
                
                if not vectors:
                    continue
                    
                # 将向量转换为NumPy数组
                vectors_array = np.array(vectors).astype('float32')
                
                # 归一化向量以提高检索质量
                faiss.normalize_L2(vectors_array)
                
                # 添加到FAISS索引
                self.index.add(vectors_array)
                batch_start_idx = start_idx + total_vectors
                
                # 更新映射并保存向量ID到数据库
                for j, chunk_id in enumerate(chunk_ids):
                    vector_idx = batch_start_idx + j
                    self.chunk_mapping[vector_idx] = chunk_id
                    
                    # 保存向量ID到数据库
                    try:
                        # 检查batch_chunks的类型并相应处理
                        if hasattr(batch_chunks, 'get'):
                            # 如果是QuerySet，使用get方法
                            chunk = batch_chunks.get(id=chunk_id)
                        else:
                            # 如果是列表或元组，使用循环查找
                            chunk = None
                            for c in batch_chunks:
                                if c.id == chunk_id:
                                    chunk = c
                                    break
                        
                        if chunk:
                            chunk.vector_id = str(vector_idx)
                            chunk.save()
                        else:
                            logger.warning(f"未找到ID为{chunk_id}的块")
                    except Exception as e:
                        logger.error(f"保存向量ID时出错: {str(e)}")
                
                # 更新已处理的向量数量
                total_vectors += len(vectors)
                
                # 手动释放内存
                del vectors
                del vectors_array
                gc.collect()
            
            # 保存索引
            self._save_index()
            
            logger.info(f"文档{document.id}的{total_vectors}个向量已成功索引")
            return True
            
        except Exception as e:
            logger.exception(f"索引文档{document.id}失败: {str(e)}")
            return False
    
    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """根据查询文本搜索相关文档块"""
        try:
            # 检查索引是否为空
            if self.index.ntotal == 0:
                logger.warning("向量索引为空，无法进行搜索")
                return []
            
            # 将查询文本转换为向量并归一化
            query_vector = self.embedding_service.get_embedding(query)
            query_vector = np.array([query_vector]).astype('float32')  # 转换为2D数组
            faiss.normalize_L2(query_vector)  # 归一化查询向量
            
            # 在向量数据库中检索最相似的向量
            distances, indices = self.index.search(query_vector, min(top_k, self.index.ntotal))
            
            # 获取检索结果
            results = []
            for i, idx in enumerate(indices[0]):  # indices是二维数组，取第一个结果
                if idx < 0:  # faiss在结果不足时会返回-1
                    continue
                    
                chunk_id = self.chunk_mapping.get(int(idx))
                if chunk_id is None:
                    continue
                    
                try:
                    chunk = DocumentChunk.objects.select_related('document_id').get(id=chunk_id)
                    document = Document.objects.get(id=chunk.document_id)
                    
                    results.append({
                        'id': document.id,
                        'title': document.title,
                        'chunk_index': chunk.chunk_index,
                        'content': chunk.content,
                        'score': float(1.0 / (1.0 + distances[0][i])),  # 将距离转换为相似度分数
                        'chunk_id': chunk_id
                    })
                except (DocumentChunk.DoesNotExist, Document.DoesNotExist):
                    # 如果文档块或文档不存在，可能是已被删除
                    continue
            
            return results
            
        except Exception as e:
            logger.exception(f"搜索失败: {str(e)}")
            return []
    
    @staticmethod
    def search_static(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """静态方法版本，适用于简单调用"""
        service = VectorDBService()
        return service.search(query, top_k)