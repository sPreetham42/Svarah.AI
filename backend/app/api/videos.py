"""Video job endpoints.

Two kinds of job share the `jobs` table:

- **AI video** (`agnes_video`): an asynchronous Agnes text-to-video task. The
  POST only submits it; the job is advanced by `GET …/videos/jobs`, which the
  client polls. All state is on the job row, so progress survives a page
  refresh and a backend restart.
- **Reel** (`video_composition`): the local FFmpeg Ken Burns composition.

Job ownership follows the campaign: a job id from another account resolves to
404, and the queued payload records the authenticated owner.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, owned_campaign
from ..errors import NotFoundError
from ..models import Job, User
from ..services.assets_service import serialize_asset
from ..services.brag_service import create_brag_video
from ..services.video_service import (
    AI_VIDEO_STAGE,
    VIDEO_JOB_STAGE,
    VIDEO_STAGES,
    cancel_video_job,
    create_ai_video_job,
    create_video_job,
    refresh_ai_video_job,
    run_video_job,
    serialize_job,
)

router = APIRouter(prefix="/campaigns", tags=["videos"])


class VideoJobRequest(BaseModel):
    profile_id: int | None = None


class AIVideoRequest(BaseModel):
    seconds: int = Field(default=5, ge=4, le=12)
    aspect_ratio: str = Field(default="9:16", max_length=8)


def _owned_job(db: Session, job_id: int, user: User, stages: tuple[str, ...]) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.stage not in stages or job.campaign_id is None:
        raise NotFoundError(f"Video job {job_id} was not found.", details={"job_id": job_id})
    owned_campaign(db, job.campaign_id, user)
    return job


@router.post("/{campaign_id}/videos/generate", status_code=202, response_model=dict)
def generate_ai_video(
    campaign_id: int,
    payload: AIVideoRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Submit an Agnes video task (202: accepted, not finished).

    A provider rejection at submit time is returned as a `failed` job so it
    stays visible in the job list. While a job is active, repeating the call
    returns that job instead of creating a second billable task.
    """
    owned_campaign(db, campaign_id, user)
    request = payload or AIVideoRequest()
    job = create_ai_video_job(
        db,
        campaign_id=campaign_id,
        owner_uid=user.owner_uid,
        seconds=request.seconds,
        aspect_ratio=request.aspect_ratio,
    )
    db.commit()
    return serialize_job(job)


@router.post("/{campaign_id}/videos/brag", status_code=201, response_model=dict)
def generate_brag_video(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Render the Brag Director's storyboard to a vertical video.

    Synchronous (a few seconds): frames are drawn in headless Chrome from the
    locked tokens and encoded with FFmpeg, with the voice-over when one exists.
    """
    owned_campaign(db, campaign_id, user)
    record = create_brag_video(db, campaign_id)
    db.commit()
    return serialize_asset(record)


@router.post("/{campaign_id}/videos/jobs", status_code=202, response_model=dict)
def queue_video_job(
    campaign_id: int,
    payload: VideoJobRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    owned_campaign(db, campaign_id, user)
    job = create_video_job(
        db,
        campaign_id=campaign_id,
        owner_uid=user.owner_uid,
        profile_id=payload.profile_id if payload else None,
    )
    db.commit()
    return serialize_job(job)


@router.get("/{campaign_id}/videos/jobs", response_model=list[dict])
def list_video_jobs(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    """Job states for the campaign, newest first.

    Polling this endpoint is what drives active AI video jobs forward: each
    call makes at most one (throttled) provider status request per active job.
    """
    owned_campaign(db, campaign_id, user)
    jobs = (
        db.query(Job)
        .filter(Job.campaign_id == campaign_id, Job.stage.in_(VIDEO_STAGES))
        .order_by(Job.id.desc())
        .all()
    )
    for job in jobs:
        if job.stage == AI_VIDEO_STAGE:
            refresh_ai_video_job(db, job)
    db.commit()
    return [serialize_job(j) for j in jobs]


@router.post("/videos/jobs/{job_id}/cancel", response_model=dict)
def cancel_video(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    job = cancel_video_job(db, _owned_job(db, job_id, user, VIDEO_STAGES))
    db.commit()
    return serialize_job(job)


@router.post("/videos/jobs/{job_id}/run", response_model=dict)
def run_video(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Synchronously drive one queued reel job (local/demo path).

    In production the durable queue consumer calls the same service function.
    """
    _owned_job(db, job_id, user, (VIDEO_JOB_STAGE,))

    result = run_video_job(db, job_id)
    # run_video_job returns (asset, job) on success or job on failure.
    if isinstance(result, tuple):
        asset, job = result
        db.commit()
        return {"job": serialize_job(job), "asset_id": asset.id}
    job = result
    db.commit()
    return {"job": serialize_job(job), "asset_id": None}
