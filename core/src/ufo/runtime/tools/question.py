"""The structured question result shared by model-authored asks and runtime permission asks."""

import json

from ufo.schema.records import AskUserInput

ASK_USER_DIRECTIVE = (
    "The member now sees a card below your reply with your title, these questions and their "
    "choices. The title says what the answers will decide; your reply reports only what you found "
    "or did before asking. Say each thing once: the reply repeats nothing on the card and never "
    "says why you ask, the title repeats nothing in the reply, and no question restates the "
    "title. Then end your turn — the answer arrives as the next message."
)


def question_result_text(question: AskUserInput) -> str:
    """Render one terminal question as the tool-result directive the turn loop recognizes."""
    payload = {
        "awaiting": "question",
        "title": question.title,
        **({"icon": question.icon} if question.icon else {}),
        "questions": [item.model_dump(exclude_none=True) for item in question.questions],
    }
    return f"{ASK_USER_DIRECTIVE}\n{json.dumps(payload)}"
