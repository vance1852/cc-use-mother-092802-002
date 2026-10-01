"""品牌权益履约领域包。

在基础服务（经营主体、操作者、站点、幂等、审计链）之上提供独立的
协议版本、权益计划、排他冲突检测、现场验收、争议冻结与费用结算能力。
"""

from .service import BrandRightsService

__all__ = ["BrandRightsService"]
