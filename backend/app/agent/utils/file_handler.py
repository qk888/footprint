import os, hashlib, re
from app.agent.utils.logger_handler import logger
from langchain_core.documents import Document
#langchain社区文档加载器，用于加载pdf和txt文件
from langchain_community.document_loaders import PyPDFLoader, CSVLoader

#文件处理工具
#获取文件的md5的十六进制字符串
def get_file_md5_hex(file_path: str):
    if not os.path.exists(file_path):
        logger.error(f"[md5计算]文件 {file_path}不存在")
        return None
    if not os.path.isfile(file_path):
        logger.error(f"[md5计算]路径 {file_path}非文件")
        return None
    
    md5_obj = hashlib.md5()

    #分片读取，避免大文件一次性进内存
    chunk_size = 4096
    try:
        #必须以二进制模式读取
        with open(file_path, "rb") as f:
            while chunk := f.read(chunk_size):
                md5_obj.update(chunk)
        return md5_obj.hexdigest()
    except Exception as e:
        logger.error(f"计算文件 {file_path} md5失败, {str(e)}")
        return None
   
#返回文件夹内的文件列表(只允许返回指定类型的文件)
def listdir_with_allowed_type(file_path: str, allowed_types: tuple[str]):
    files = []
    if not os.path.isdir(file_path):
        logger.error(f"[文件列表]路径 {file_path}非文件夹")
        return ()
    
    for f in os.listdir(file_path):
        if f.endswith(allowed_types):
            files.append(os.path.join(file_path, f))
    return tuple(files)

def pdf_loader(file_path: str, password: str = None) -> list[Document]:
    return PyPDFLoader(file_path, password=password).load()

def txt_loader_by_city(file_path: str) -> list[Document]:
    """按 ### 标记切分攻略 txt，每个块一个 Document。

    块头形如 "重庆 · 了解：正文..."（维基导游知识库）或 "重庆\\n正文..."（早期手写攻略）。
    把 title/section 解析进 metadata —— 检索时靠它做 where={"title": ...} 的城市过滤，
    这是"先定位城市、再在该城范围内检索"的前提：
    全局检索会被同词干扰（实测 "重庆三日游攻略" 的全局 top1 会是西安），限定城市后命中率 86%→100%。
    """
    with open(file_path, encoding="utf-8") as f:
        text = f.read()

    docs = []
    for part in text.split("###"):
        part = part.strip()
        if not part:
            continue
        # 头部：取到第一个中文冒号或换行为止
        head = re.split(r"[：\n]", part, 1)[0].strip()
        meta = {}
        if 0 < len(head) <= 30:
            if " · " in head:
                title, section = head.split(" · ", 1)
            else:
                title, section = head, ""
            meta = {"title": title.strip(), "section": section.strip()}
        docs.append(Document(page_content=part, metadata=meta))
    return docs

def csv_loader(file_path: str) -> list[Document]:
    return CSVLoader(file_path, encoding="utf-8").load()
