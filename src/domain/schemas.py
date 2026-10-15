from pydantic import BaseModel, ConfigDict, Field


class Episode(BaseModel):
    """
    Bangumi Episode 数据模型
    用于校验和解析从 API 返回的剧集信息
    """

    airdate: str | None = Field(None, description="播出日期,格式: YYYY-MM-DD")
    name: str = Field(..., description="剧集日文名称")
    name_cn: str = Field(..., description="剧集中文名称")
    duration: str | None = Field(None, description="时长,格式: HH:MM:SS")
    desc: str = Field(default="", description="剧集简介")
    ep: int = Field(..., description="集数", ge=0)
    sort: float = Field(..., description="排序号(特别篇可能为小数)", ge=0)
    id: int = Field(..., description="剧集ID")
    subject_id: int = Field(..., description="条目ID")
    comment: int = Field(default=0, description="评论数", ge=0)
    type: int = Field(..., description="剧集类型")
    disc: int = Field(default=0, description="碟片号", ge=0)
    duration_seconds: int | None = Field(None, description="时长(秒)", ge=0)

    # 允许额外字段,API 可能返回更多数据
    model_config = ConfigDict(extra="allow")
