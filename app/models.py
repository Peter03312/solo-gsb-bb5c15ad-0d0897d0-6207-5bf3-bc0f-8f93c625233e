"""请求体模型。"""
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class StratumIn(BaseModel):
    stratum_id: str = Field(min_length=1, max_length=128, description="地层编号，全库唯一")
    site: Optional[str] = Field(default=None, max_length=256, description="遗址名称")


class AssertOp(BaseModel):
    """新增断言。relation=earlier_than 表示 stratum_a 早于 stratum_b。"""

    op: Literal["assert"]
    evidence_id: str = Field(min_length=1, max_length=128, description="证据编号，全库唯一（含已撤回）")
    stratum_a: str = Field(min_length=1, max_length=128)
    stratum_b: str = Field(min_length=1, max_length=128)
    relation: Literal["same_period", "earlier_than"]


class RetractOp(BaseModel):
    """撤回断言（按证据编号，仅可撤回生效中的断言）。"""

    op: Literal["retract"]
    evidence_id: str = Field(min_length=1, max_length=128)


Operation = Annotated[Union[AssertOp, RetractOp], Field(discriminator="op")]


class BatchIn(BaseModel):
    """一批断言变更。须携带当前修订号；整批按应用后的最终关系核验，
    任一冲突则整批拒绝、原状态不变；全部通过则原子递增修订号。"""

    expected_revision: int = Field(ge=0, description="当前修订号；不一致则整批拒绝")
    operations: List[Operation] = Field(min_length=1)


class SnapshotIn(BaseModel):
    label: Optional[str] = Field(default=None, max_length=256, description="版本备注")


class SimulationIn(BaseModel):
    """创建推演：固定当前正式修订号、已登记地层与生效断言；不改动任何正式数据。"""

    sim_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="推演编号；省略则服务端生成 SIM-001 形式编号",
    )
    label: Optional[str] = Field(default=None, max_length=256, description="推演备注")


class SimBatchIn(BaseModel):
    """推演内一批断言变更。沿用正式批次语义，但携带的是推演修订号，
    整批按应用后的最终关系核验，任一冲突整批拒绝；通过则原子递增推演修订号。"""

    expected_sim_revision: int = Field(ge=0, description="当前推演修订号；不一致则整批拒绝")
    operations: List[Operation] = Field(min_length=1)
