from pydantic import BaseModel, Field

#agent工具校验
class SaveMemoryInput(BaseModel):
    content: str = Field(description="要长期记住的内容，写成一句完整的中文陈述，例如：用户不吃辣")

class RecallMemoryInput(BaseModel):
    keyword: str = Field(description="回忆记忆用的关键词，例如：辣、住宿、预算")
