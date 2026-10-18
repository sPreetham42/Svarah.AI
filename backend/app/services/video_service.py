"""Video generation job service (Phase 6, handoff §4).

Reliable core: deterministic FFmpeg reel built from the verified poster +
asset audio — Ken Burns zoom on stills with end-card. Agnes Video is the
optional enhancement and is gated behind configuration AND the deliberate
interface-verification toggle; jobs record per-stage state in the ``jobs``
table (``JobStatus`` from the source of truth §24).
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess  # nosec - argv-array invocation only, never shell strings
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ConflictError, NotFoundError, TitanError, ValidationError
from ..models import AssetRecord, Campaign, FactSheetRecord, Job, JobStatus, utcnow
from ..services.checksums import sha256_digest
from ..services.fact_engine import substitute_tokens
from ..services.campaign_brain import get_plan_record

VIDEO_JOB_STAGE = "video_composition"


def _audit(db: Session, campaign_id: int, event_type: str, payload: dict[str, Any] | None = None) -> None:
    db.add(
        __import__("app.models", fromlist=["AuditEvent"]).AuditEvent(
            campaign_id=campaign_id,
            event_type=event_type,
            payload_json=json.dumps(payload) if payload else None,
        )
    )


def _ffmpeg_binary() -> str:
    path = _resolved_ffmpeg()
    if path is None:
        raise ValidationError(
            "FFmpeg is not available on this system.", code="ffmpeg_unavailable"
        )
    return path


def _ffprobe_binary() -> str:
    path = _resolved_ffprobe()
    if path is None:
        raise ValidationError(
            "ffprobe is not available on this system.", code="ffprobe_unavailable"
        )
    return path


@functools.lru_cache(maxsize=1)
def _resolved_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # optional bundled binary (pip install imageio-ffmpeg)
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - not installed / no binary for this platform
        return None


@functools.lru_cache(maxsize=1)
def _resolved_ffprobe() -> str | None:
    return shutil.which("ffprobe")


def probe_video(path: Path) -> dict[str, Any]:
    """ffprobe the produced reel; raises on invalid/unprobeable media."""
    if _resolved_ffprobe() is None and _resolved_ffmpeg() is not None:
        return _probe_with_ffmpeg(path)
    ffprobe = _ffprobe_binary()
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidationError("ffprobe timed out.", code="media_probe_timeout") from exc
    if completed.returncode != 0:
        raise ValidationError(
            "ffprobe could not read the produced video.",
            code="media_invalid",
            details={"stderr": completed.stderr.decode("utf-8", "replace")[:400]},
        )
    return json.loads(completed.stdout or "{}")


def _probe_with_ffmpeg(path: Path) -> dict[str, Any]:
    """ffprobe stand-in for installs that ship only the ffmpeg binary.

    `ffmpeg -i` prints the container summary; a file with no video stream or
    no readable duration is rejected exactly as ffprobe would reject it.
    """
    import re

    completed = subprocess.run(
        [_ffmpeg_binary(), "-hide_banner", "-i", str(path)], capture_output=True, timeout=60
    )
    text = completed.stderr.decode("utf-8", "replace")
    duration = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    streams = re.findall(r"Stream #\d+:\d+.*?: (Video|Audio)", text)
    if duration is None or "Video" not in streams:
        raise ValidationError(
            "The produced video could not be read back.",
            code="media_invalid",
            details={"stderr": text[-400:]},
        )
    hours, minutes, seconds = duration.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return {
        "format": {"duration": f"{total:.3f}"},
        "streams": [{"codec_type": kind.lower()} for kind in streams],
    }


def build_reel_ffmpeg(
    *,
    poster_png: bytes,
    voice_wav: bytes | None,
    headline: str,
    output_path: Path,
) -> bytes:
    """Deterministic Ken Burns reel: poster still (zoompan) + optional audio."""
    ffmpeg = _ffmpeg_binary()
    duration = 6.0
    with tempfile.TemporaryDirectory(prefix="titan-reel-") as tmp:
        tmp_dir = Path(tmp)
        poster_path = tmp_dir / "poster.png"
        poster_path.write_bytes(poster_png)
        args: list[str] = ["-y", "-loop", "1", "-i", str(poster_path)]
        if voice_wav:
            voice_path = tmp_dir / "voice.wav"
            voice_path.write_bytes(voice_wav)
            args += ["-i", str(voice_path)]
        filter_graph = (
            # Fit the 4:5 poster inside the 9:16 frame (never stretch it).
            "scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=0x141513,"
            "zoompan=z='min(zoom+0.0008,1.08)':d=180:s=720x1280:fps=30"
        )
        args += ["-vf", filter_graph, "-t", str(duration), "-r", "30", "-pix_fmt", "yuv420p"]
        if voice_wav:
            args += ["-c:v", "libx264", "-c:a", "aac", "-shortest"]
        else:
            args += ["-c:v", "libx264"]
        args.append(str(output_path))
        try:
            completed = subprocess.run([ffmpeg, *args], capture_output=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            raise ValidationError("FFmpeg composition timed out.", code="ffmpeg_timeout") from exc
        except FileNotFoundError as exc:
            raise ValidationError(
                "FFmpeg is not available on this system.",
                code="ffmpeg_unavailable",
            ) from exc
        if completed.returncode != 0:
            raise ValidationError(
                "FFmpeg composition failed.",
                code="ffmpeg_failed",
                details={"stderr": completed.stderr.decode("utf-8", "replace")[:500]},
            )
        return output_path.read_bytes()


def create_video_job(db: Session, *, campaign_id: int, owner_uid: str, profile_id: int | None = None) -> Job:
    """Queue a reel composition job backed by the verified poster + voice."""
    settings = get_settings()
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError(f"Campaign {campaign_id} was not found.")
    plan_record = get_plan_record(db, campaign_id)
    if plan_record is None:
        raise ConflictError("Generate the campaign plan first (POST /plan).", code="plan_missing")
    posters = (
        db.query(AssetRecord)
        .filter(AssetRecord.campaign_id == campaign_id, AssetRecord.kind == "poster")
        .all()
    )
    if not posters:
        raise ConflictError(
            "Generate a poster asset first (POST /assets/posters).",
            code="poster_missing",
        )
    existing_jobs = (
        db.query(Job)
        .filter(Job.campaign_id == campaign_id, Job.stage == VIDEO_JOB_STAGE, Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)))
        .count()
    )
    total_videos = (
        db.query(AssetRecord)
        .filter(AssetRecord.campaign_id == campaign_id, AssetRecord.kind == "video")
        .count()
    )
    if total_videos + existing_jobs >= settings.max_video_jobs:
        raise ConflictError(
            f"Video budget reached ({settings.max_video_jobs}).", code="budget_exceeded"
        )
    job = Job(
        campaign_id=campaign_id,
        stage=VIDEO_JOB_STAGE,
        status=JobStatus.QUEUED,
        payload_json=json.dumps({"owner_uid": owner_uid, "profile_id": profile_id, "poster_asset_id": posters[-1].id}),
    )
    db.add(job)
    db.flush()
    _audit(db, campaign_id, "job.video_queued", {"job_id": job.id})
    return job


def run_video_job(db: Session, job_id: int) -> Job:
    """Execute one queued video job synchronously (worker entry point).

    Production note: this function is deliberately provider-independent and
    idempotent-safe to call from a durable queue consumer (Cloud Tasks →
    Cloud Run worker). Job state transitions are persisted at every step.
    """
    job = db.get(Job, job_id)
    if job is None or job.stage != VIDEO_JOB_STAGE:
        raise NotFoundError(f"Video job {job_id} was not found.")
    if job.status in (JobStatus.COMPLETED,):
        return job
    payload = json.loads(job.payload_json or "{}")
    poster_asset = db.get(AssetRecord, payload.get("poster_asset_id"))
    if poster_asset is None or not poster_asset.storage_path:
        job.status = JobStatus.FAILED
        job.error = "Poster asset for reel is missing."
        db.flush()
        return job

    job.status = JobStatus.RUNNING
    job.started_at = utcnow()
    job.attempts += 1
    db.flush()

    from ..storage import get_storage

    storage = get_storage()
    poster_png = storage.read_bytes(poster_asset.storage_path)
    voice_record = (
        db.query(AssetRecord)
        .filter(
            AssetRecord.campaign_id == job.campaign_id,
            AssetRecord.kind == "voice",
        )
        .order_by(AssetRecord.id.desc())
        .first()
    )
    voice_wav = None
    if voice_record and voice_record.storage_path:
        voice_wav = storage.read_bytes(voice_record.storage_path)

    sheet = db.get(FactSheetRecord, poster_asset.factsheet_id)
    tokens = json.loads(sheet.tokens_json or "{}") if sheet else {}
    headline = ""
    if tokens.get("DISCOUNT") and tokens.get("PRODUCT"):
        joiner = "on" if tokens["DISCOUNT"].rstrip().lower().endswith("off") else "off"
        headline = substitute_tokens(f"{{{{DISCOUNT}}}} {joiner} {{{{PRODUCT}}}}", tokens)

    try:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="titan-video-") as tmp:
            output_path = Path(tmp) / "reel.mp4"
            build_reel_ffmpeg(
                poster_png=poster_png,
                voice_wav=voice_wav,
                headline=headline,
                output_path=output_path,
            )
            probe = probe_video(output_path)
            video_bytes = output_path.read_bytes()
    except ValidationError as exc:
        job.status = JobStatus.FAILED
        job.error = exc.message
        db.flush()
        _audit(db, job.campaign_id, "job.video_failed", {"job_id": job.id, "code": exc.code})
        return job

    relative_path = f"campaigns/{job.campaign_id}/videos/reel_{job.id}.mp4"
    storage.save_bytes(video_bytes, relative_path)
    sha = sha256_digest(video_bytes)
    record = AssetRecord(
        campaign_id=job.campaign_id,
        plan_id=poster_asset.plan_id,
        factsheet_id=poster_asset.factsheet_id,
        fact_hash=poster_asset.fact_hash,
        kind="video",
        locale=voice_record.locale if voice_record else "en-IN",
        storage_path=relative_path,
        sha256=sha,
        asset_status="validating",
        provider="ffmpeg",
        model="kenburns-v1",
        is_mock=False,
        used_fallback=False,
        provenance_json=json.dumps(
            {
                "poster_asset_id": poster_asset.id,
                "voice_asset_id": voice_record.id if voice_record else None,
                "voice_is_mock": bool(voice_record and voice_record.is_mock),
                "duration_seconds": probe.get("format", {}).get("duration"),
                "streams": len(probe.get("streams") or []),
                "engine": "ffmpeg-kenburns-v1",
            },
            ensure_ascii=False,
        ),
    )
    db.add(record)
    db.flush()
    payload["asset_id"] = record.id
    job.payload_json = json.dumps(payload)
    job.status = JobStatus.COMPLETED
    job.completed_at = utcnow()
    job.error = None
    db.flush()
    _audit(db, job.campaign_id, "job.video_completed", {"job_id": job.id, "asset_id": record.id})
    return record, job


# ── Agnes AI video (asynchronous provider jobs) ───────────────────────
AI_VIDEO_STAGE = "agnes_video"
VIDEO_STAGES = (VIDEO_JOB_STAGE, AI_VIDEO_STAGE)

#: Never ask the provider more often than this, however often the UI polls.
AI_VIDEO_POLL_INTERVAL = 4.0
#: A task still unfinished after this long is failed locally (the provider
#: task id stays on the job for support).
AI_VIDEO_DEADLINE = 15 * 60.0
#: Consecutive transient poll failures tolerated before giving up.
AI_VIDEO_MAX_POLL_ERRORS = 8
#: A download that started this long ago without finishing is retried.
AI_VIDEO_DOWNLOAD_STALE = 5 * 60.0
AI_VIDEO_MAX_BYTES = 200 * 1024 * 1024
AI_VIDEO_SIZE = "720P"
AI_VIDEO_ASPECT_RATIOS = ("9:16", "16:9", "1:1", "4:3", "3:4", "21:9")
AI_VIDEO_MIN_SECONDS, AI_VIDEO_MAX_SECONDS = 4, 12

_ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.VALIDATING)
#: Poll errors that will not get better by asking again.
_PERMANENT_POLL_CODES = (
    "provider_auth_error",
    "provider_quota_exceeded",
    "provider_bad_request",
    "provider_not_configured",
    "video_not_found",
    "video_disabled",
    "video_bad_response",
)


def _now() -> float:
    import time

    return time.time()


def _payload(job: Job) -> dict[str, Any]:
    try:
        value = json.loads(job.payload_json or "{}")
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _save_payload(job: Job, payload: dict[str, Any]) -> None:
    job.payload_json = json.dumps(payload, ensure_ascii=False)


def build_video_prompt(plan, tokens: dict[str, str]) -> str:
    """The Director's video brief, grounded in descriptive facts only.

    Numbers, prices and dates are deliberately left out: a video model cannot
    be trusted to render them, and the verified poster/captions carry them.
    """
    base = str((plan.video_brief or {}).get("prompt") or "").strip() or (
        "Slow cinematic push-in on a welcoming small local shop, warm lighting, "
        "a happy customer being served."
    )
    parts = [base]
    if tokens.get("PRODUCT"):
        parts.append(f"The shop is promoting {tokens['PRODUCT']}.")
    if tokens.get("AUDIENCE"):
        parts.append(f"Customers shown: {tokens['AUDIENCE']}.")
    if tokens.get("LOCATION"):
        parts.append(f"Setting: {tokens['LOCATION']}, India.")
    parts.append("No on-screen text, no captions, no numbers, no logos, no watermark.")
    return " ".join(parts)


def _fail(db: Session, job: Job, message: str, *, code: str) -> Job:
    job.status = JobStatus.FAILED
    job.error = message[:1000]
    job.completed_at = utcnow()
    payload = _payload(job)
    payload["error_code"] = code
    _save_payload(job, payload)
    db.flush()
    _audit(db, job.campaign_id, "job.video_failed", {"job_id": job.id, "code": code})
    return job


def create_ai_video_job(
    db: Session,
    *,
    campaign_id: int,
    owner_uid: str,
    seconds: int = 5,
    aspect_ratio: str = "9:16",
) -> Job:
    """Submit an Agnes text-to-video task and persist it as a job.

    The provider is asynchronous: this only *creates* the task. The job stays
    ``running`` until `refresh_ai_video_job` sees a terminal provider status.
    Repeating the request while a job is active returns that job (no duplicate
    billable task).
    """
    from .agnes import get_video_client
    from .async_bridge import run_sync
    from .campaign_brain import _plan_from_record

    settings = get_settings()
    if not AI_VIDEO_MIN_SECONDS <= seconds <= AI_VIDEO_MAX_SECONDS:
        raise ValidationError(
            f"Video length must be {AI_VIDEO_MIN_SECONDS}–{AI_VIDEO_MAX_SECONDS} seconds.",
            code="invalid_duration",
        )
    if aspect_ratio not in AI_VIDEO_ASPECT_RATIOS:
        raise ValidationError(
            f"Aspect ratio must be one of {', '.join(AI_VIDEO_ASPECT_RATIOS)}.",
            code="invalid_aspect_ratio",
        )
    if db.get(Campaign, campaign_id) is None:
        raise NotFoundError(f"Campaign {campaign_id} was not found.")
    plan_record = get_plan_record(db, campaign_id)
    if plan_record is None:
        raise ConflictError("Generate the campaign plan first (POST /plan).", code="plan_missing")

    jobs = (
        db.query(Job)
        .filter(Job.campaign_id == campaign_id, Job.stage == AI_VIDEO_STAGE)
        .order_by(Job.id.desc())
        .all()
    )
    active = next((j for j in jobs if j.status in _ACTIVE), None)
    if active is not None:
        return active
    used = sum(1 for j in jobs if j.status == JobStatus.COMPLETED)
    if used >= settings.max_video_variants:
        raise ConflictError(
            f"AI video budget reached ({settings.max_video_variants}).", code="budget_exceeded"
        )

    client = get_video_client(settings)  # disabled / unconfigured → visible error
    sheet = db.get(FactSheetRecord, plan_record.fact_sheet_id)
    tokens = json.loads(sheet.tokens_json or "{}") if sheet else {}
    plan = _plan_from_record(plan_record, tokens)
    prompt = build_video_prompt(plan, tokens)

    job = Job(
        campaign_id=campaign_id,
        stage=AI_VIDEO_STAGE,
        status=JobStatus.QUEUED,
        provider=client.name,
        payload_json=json.dumps(
            {
                "owner_uid": owner_uid,
                "prompt": prompt,
                "seconds": seconds,
                "size": AI_VIDEO_SIZE,
                "aspect_ratio": aspect_ratio,
                "plan_id": plan_record.id,
                "factsheet_id": plan_record.fact_sheet_id,
                "fact_hash": plan_record.fact_hash,
                "progress": 0,
            },
            ensure_ascii=False,
        ),
    )
    db.add(job)
    db.flush()
    _audit(db, campaign_id, "job.video_queued", {"job_id": job.id, "engine": client.name})

    job.attempts += 1
    try:
        task = run_sync(
            client.create(prompt, seconds=seconds, size=AI_VIDEO_SIZE, aspect_ratio=aspect_ratio)
        )
    except TitanError as exc:
        # Persist the failure so it is visible on the job list after a reload.
        return _fail(db, job, exc.message, code=exc.code)

    payload = _payload(job)
    payload.update(
        {
            "video_id": task.video_id,
            "provider_status": task.status,
            "progress": task.progress or 0,
            "is_mock": task.is_mock,
            "submitted_at": _now(),
            "last_polled_at": _now(),
            "poll_errors": 0,
        }
    )
    _save_payload(job, payload)
    job.model = task.model
    job.status = JobStatus.RUNNING
    job.started_at = utcnow()
    db.flush()
    _audit(db, campaign_id, "job.video_submitted", {"job_id": job.id})
    return job


def _looks_like_video(data: bytes) -> str | None:
    """Container sniff: returns a file suffix, or None for non-video bytes."""
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"\x1aE\xdf\xa3":
        return ".webm"
    return None


def refresh_ai_video_job(db: Session, job: Job, *, force: bool = False) -> Job:
    """Advance one AI video job by at most one provider poll.

    Safe to call on every status request: it is throttled, bounded by a
    deadline, and all state lives on the job row — so progress survives a page
    refresh or a backend restart. Success is recorded only after the video has
    been downloaded and recognised as a video file.
    """
    from .agnes import (
        VIDEO_STATUS_COMPLETED,
        VIDEO_STATUS_FAILED,
        get_video_client,
    )
    from .async_bridge import run_sync

    if job.stage != AI_VIDEO_STAGE or job.status not in (JobStatus.RUNNING, JobStatus.VALIDATING):
        return job
    payload = _payload(job)
    now = _now()

    if job.status == JobStatus.VALIDATING:
        # Another request is downloading. Only take over if it clearly died.
        if now - float(payload.get("download_started_at") or 0) < AI_VIDEO_DOWNLOAD_STALE:
            return job
        job.status = JobStatus.RUNNING

    video_id = payload.get("video_id")
    if not video_id:
        return _fail(db, job, "The provider task id was lost; start a new video.", code="video_id_missing")
    submitted = float(payload.get("submitted_at") or now)
    if now - submitted > AI_VIDEO_DEADLINE:
        return _fail(
            db,
            job,
            f"The video was not ready after {int(AI_VIDEO_DEADLINE // 60)} minutes "
            f"(provider task {video_id}). Start a new one.",
            code="video_deadline_exceeded",
        )
    if not force and now - float(payload.get("last_polled_at") or 0) < AI_VIDEO_POLL_INTERVAL:
        return job

    payload["last_polled_at"] = now
    try:
        client = get_video_client(get_settings())
        task = run_sync(client.retrieve(str(video_id)))
    except TitanError as exc:
        errors = int(payload.get("poll_errors") or 0) + 1
        payload["poll_errors"] = errors
        payload["last_poll_error"] = exc.message
        _save_payload(job, payload)
        if exc.code in _PERMANENT_POLL_CODES or errors >= AI_VIDEO_MAX_POLL_ERRORS:
            return _fail(db, job, f"Status check failed: {exc.message}", code=exc.code)
        db.flush()
        return job  # transient: stay running, try again on the next poll

    payload["poll_errors"] = 0
    payload.pop("last_poll_error", None)
    payload["video_id"] = task.video_id
    payload["provider_status"] = task.status
    if task.progress is not None:
        payload["progress"] = max(int(payload.get("progress") or 0), min(task.progress, 99))
    _save_payload(job, payload)

    if task.status == VIDEO_STATUS_FAILED:
        return _fail(
            db, job, task.error or "The provider reported that video generation failed.",
            code="video_generation_failed",
        )
    if task.status != VIDEO_STATUS_COMPLETED:
        db.flush()
        return job
    if not task.url:
        return _fail(
            db, job, "The provider finished but returned no video URL.", code="video_url_missing"
        )

    # Claim the download before starting it, so a concurrent poll cannot
    # fetch and store the same video twice.
    payload["download_started_at"] = now
    _save_payload(job, payload)
    job.status = JobStatus.VALIDATING
    db.commit()

    try:
        data = run_sync(client.download(task.url, max_bytes=AI_VIDEO_MAX_BYTES))
    except TitanError as exc:
        return _fail(db, job, exc.message, code=exc.code)
    suffix = _looks_like_video(data)
    if suffix is None:
        return _fail(
            db, job, "The downloaded file is not a recognisable video.", code="video_invalid"
        )

    from ..storage import get_storage

    relative_path = f"campaigns/{job.campaign_id}/videos/ai_{job.id}{suffix}"
    get_storage().save_bytes(data, relative_path)
    record = AssetRecord(
        campaign_id=job.campaign_id,
        plan_id=payload.get("plan_id"),
        factsheet_id=payload.get("factsheet_id"),
        fact_hash=payload.get("fact_hash"),
        kind="video",
        locale="en-IN",
        text_content=None,
        storage_path=relative_path,
        sha256=sha256_digest(data),
        asset_status="validating",
        provider=task.provider,
        model=task.model,
        provider_request_id=str(task.video_id)[:128],
        is_mock=task.is_mock,
        used_fallback=False,
        provenance_json=json.dumps(
            {
                "engine": task.provider if not task.is_mock else "mock_video",
                "job_id": job.id,
                "prompt": payload.get("prompt"),
                "seconds": payload.get("seconds"),
                "size": payload.get("size"),
                "aspect_ratio": payload.get("aspect_ratio"),
                "bytes": len(data),
                "playable": not task.is_mock,
            },
            ensure_ascii=False,
        ),
    )
    db.add(record)
    db.flush()
    payload["asset_id"] = record.id
    payload["progress"] = 100
    _save_payload(job, payload)
    job.status = JobStatus.COMPLETED
    job.completed_at = utcnow()
    job.error = None
    db.flush()
    _audit(db, job.campaign_id, "job.video_completed", {"job_id": job.id, "asset_id": record.id})
    return job


def cancel_video_job(db: Session, job: Job) -> Job:
    """Stop tracking an active job. (Agnes documents no task-cancel call, so a
    task already submitted may still finish on the provider's side.)"""
    if job.status not in _ACTIVE:
        raise ConflictError(
            f"Job {job.id} is already {job.status} and cannot be cancelled.",
            code="job_not_active",
        )
    job.status = JobStatus.CANCELLED
    job.completed_at = utcnow()
    job.error = "Cancelled by the owner."
    db.flush()
    _audit(db, job.campaign_id, "job.video_cancelled", {"job_id": job.id})
    return job


def serialize_job(job: Job) -> dict[str, Any]:
    payload = _payload(job)
    is_ai = job.stage == AI_VIDEO_STAGE
    progress = payload.get("progress")
    if job.status == JobStatus.COMPLETED:
        progress = 100
    return {
        "id": job.id,
        "campaign_id": job.campaign_id,
        "stage": job.stage,
        "kind": "ai_video" if is_ai else "reel",
        "status": job.status,
        "active": job.status in _ACTIVE,
        "attempts": job.attempts,
        "error": job.error,
        "error_code": payload.get("error_code"),
        "provider": job.provider or (None if is_ai else "ffmpeg"),
        "model": job.model,
        "provider_status": payload.get("provider_status"),
        "progress": progress if isinstance(progress, (int, float)) else None,
        "asset_id": payload.get("asset_id"),
        "is_mock": bool(payload.get("is_mock")),
        "seconds": payload.get("seconds"),
        "aspect_ratio": payload.get("aspect_ratio"),
        "created_at": str(job.created_at),
        "started_at": str(job.started_at) if job.started_at else None,
        "completed_at": str(job.completed_at) if job.completed_at else None,
    }


__all__ = [
    "AI_VIDEO_STAGE",
    "VIDEO_JOB_STAGE",
    "VIDEO_STAGES",
    "build_reel_ffmpeg",
    "build_video_prompt",
    "cancel_video_job",
    "create_ai_video_job",
    "create_video_job",
    "probe_video",
    "refresh_ai_video_job",
    "run_video_job",
    "serialize_job",
]
