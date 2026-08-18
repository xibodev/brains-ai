from pydantic import BaseModel


class Classification(BaseModel):
    task_type: str
    complexity: str
    needs_codebase: bool
    needs_external_docs: bool
    needs_memory: bool
    freshness_required: bool
    recommended_model_tier: str
    confidence: float
