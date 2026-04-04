from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pydantic.networks import EmailStr

from app.api.deps import get_current_active_superuser, CurrentUser, SessionDep
from app.models import Message
from app.utils import generate_test_email, send_email
from app.services.ai_functions.ai_functions_service import AIFunctionsService

router = APIRouter(prefix="/utils", tags=["utils"])


class RefinePromptRequest(BaseModel):
    """Request body for prompt refinement."""
    user_input: str
    has_files_attached: bool = False
    agent_id: str | None = None
    mode: str = "conversation"
    is_new_agent: bool = False


class RefinePromptResponse(BaseModel):
    """Response from prompt refinement."""
    success: bool
    refined_prompt: str | None = None
    error: str | None = None


@router.post(
    "/test-email/",
    dependencies=[Depends(get_current_active_superuser)],
    status_code=201,
)
def test_email(email_to: EmailStr) -> Message:
    """
    Test emails.
    """
    email_data = generate_test_email(email_to=email_to)
    send_email(
        email_to=email_to,
        subject=email_data.subject,
        html_content=email_data.html_content,
    )
    return Message(message="Test email sent")


@router.get("/health-check/")
async def health_check() -> bool:
    return True


@router.post("/refine-prompt/")
def refine_prompt(
    request: RefinePromptRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> RefinePromptResponse:
    """
    Refine a user's prompt using AI to make it more effective.

    Requires authentication.
    """
    if not AIFunctionsService.is_available():
        raise HTTPException(
            status_code=503,
            detail="AI functions are not available. Please configure GOOGLE_API_KEY.",
        )

    from uuid import UUID
    agent_id = UUID(request.agent_id) if request.agent_id else None

    result = AIFunctionsService.refine_user_prompt(
        db=session,
        user_input=request.user_input,
        has_files_attached=request.has_files_attached,
        agent_id=agent_id,
        owner_id=current_user.id,
        mode=request.mode,
        is_new_agent=request.is_new_agent,
    )

    return RefinePromptResponse(
        success=result.get("success", False),
        refined_prompt=result.get("refined_prompt"),
        error=result.get("error"),
    )
