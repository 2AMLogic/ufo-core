from pydantic import BaseModel, Field

SUBAGENT_NAME = "sample_probe"


class ProbeTask(BaseModel):
    task: str = Field(
        description="Freeform task governed by the shared delivery register.",
    )


class ProbeFinding(BaseModel):
    finding: str
