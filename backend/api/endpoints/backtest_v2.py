from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from pandas.errors import EmptyDataError
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from .backtest_v2_artifacts import stabilize_experiment_result

router = APIRouter(tags=["backtest_v2"])

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _jobs_root() -> Path:
    root = _bt_root() / "results" / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _job_file(job_id: str) -> Path:
    return _jobs_root() / f"{job_id}.json"


def _persist_job(job: dict[str, Any]) -> None:
    try:
        _job_file(str(job.get("job_id"))).write_text(
            json.dumps(_sanitize_for_json(job), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_job_from_disk(job_id: str) -> dict[str, Any] | None:
    p = _job_file(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


class BacktestV2RunRequest(BaseModel):
    start_date: str = "2021-01-01"
    end_date: str = "2026-02-01"
    symbols: list[str] = Field(default_factory=lambda: ["600000.SH", "000001.SZ", "600519.SH"])
    benchmark: str = "000300.SH"
    compare_symbol: str = "000001.SH"
    initial_capital: float = 100000.0
    fast: int = 5
    slow: int = 20
    order_size: int = 1000
    execution_profile: str = "basic"
    sizing_mode: str = "fixed_qty"
    order_value: float = 10000.0
    reserve_cash_ratio: float = 0.02
    allow_pyramiding: bool = False
    force_run: bool = False
    validation_request_id: str | None = None
    validation_passed: bool | None = None
    validation_warnings: list[str] | None = None


class BacktestV2ExperimentRequest(BaseModel):
    preset: str = "G1-G4"
    symbols: list[str] | None = None
    start_date: str | None = None
    end_date: str | None = None
    benchmark: str | None = None
    compare_symbol: str | None = None
    initial_capital: float | None = None
    max_positions: int | None = None
    base_order_value: float | None = None
    univote_lot_mode: str | None = None
    lot_size: int | None = None
    universe_limit: int | None = None
    fast_mode: bool | None = None
    config_path: str | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _bt_root() -> Path:
    env_root = os.environ.get("BACKTEST_V2_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    return (_repo_root() / "backtest_V2").resolve()


def _load_base_config(bt_root: Path) -> dict[str, Any]:
    cfg_path = bt_root / "configs" / "pams_cn_pg.json"
    if not cfg_path.exists():
        raise HTTPException(status_code=500, detail=f"missing config template: {cfg_path}")
    return json.loads(cfg_path.read_text(encoding="utf-8-sig"))


def _build_runtime_config(bt_root: Path, body: BacktestV2RunRequest) -> tuple[Path, str]:
    cfg = _load_base_config(bt_root)
    cfg["universe"]["start"] = body.start_date
    cfg["universe"]["end"] = body.end_date
    cfg["universe"]["symbols"] = body.symbols
    cfg["universe"]["benchmark"] = body.benchmark
    cfg["universe"]["benchmark_compare_symbol"] = body.compare_symbol
    cfg["initial_capital"] = float(body.initial_capital)
    cfg["strategy"]["fast"] = int(body.fast)
    cfg["strategy"]["slow"] = int(body.slow)
    cfg["strategy"]["order_size"] = int(body.order_size)
    cfg.setdefault("execution", {})
    cfg["execution"]["profile"] = str(body.execution_profile)
    cfg["execution"]["allow_pyramiding"] = bool(body.allow_pyramiding)
    cfg.setdefault("sizing", {})
    cfg["sizing"]["mode"] = str(body.sizing_mode)
    cfg["sizing"]["order_value"] = float(body.order_value)
    cfg["sizing"]["reserve_cash_ratio"] = float(body.reserve_cash_ratio)

    run_id = f"run_{uuid.uuid4().hex[:10]}"
    cfg_path = bt_root / "configs" / f".tmp_{run_id}.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path, run_id


def _run_backtest_v2(bt_root: Path, cfg_path: Path, run_id: str) -> Path:
    out_dir = bt_root / "results" / run_id
    env = os.environ.copy()
    env["PAMS_PG_HOST"] = env.get("PGHOST", env.get("PAMS_PG_HOST", "pams-postgres"))
    env["PAMS_PG_PORT"] = env.get("PGPORT", env.get("PAMS_PG_PORT", "5432"))
    env["PAMS_PG_DBNAME"] = env.get("PGDATABASE", env.get("PAMS_PG_DBNAME", "pams"))
    env["PAMS_PG_USER"] = env.get("PGUSER", env.get("PAMS_PG_USER", "pams"))
    env["PAMS_PG_PASSWORD"] = env.get("PGPASSWORD", env.get("PAMS_PG_PASSWORD", ""))

    cmd = [
        sys.executable,
        "-m",
        "backtest_v2.cli.main",
        "run",
        "--config",
        str(cfg_path),
        "--out",
        str(out_dir),
    ]
    proc = subprocess.run(cmd, cwd=str(bt_root), env=env, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"backtest_v2 run failed: {proc.stderr.strip() or proc.stdout.strip()}",
        )
    return out_dir


def _read_validation_audit(bt_root: Path, request_id: str) -> dict[str, Any] | None:
    p = bt_root / "results" / request_id / "audit" / "validate_data.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _validation_warnings_to_strings(value: Any) -> list[str]:
    if value is None:
        return []
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                code = str(item.get("code", "")).strip()
                msg = str(item.get("message", "")).strip()
                out.append(f"{code}: {msg}" if code else (msg or json.dumps(item, ensure_ascii=False)))
            else:
                out.append(str(item))
    return out


def _write_data_validation_section(out_dir: Path, body: BacktestV2RunRequest, bt_root: Path) -> None:
    report_path = out_dir / "report.json"
    if not report_path.exists():
        return
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))

    audit = None
    if body.validation_request_id:
        audit = _read_validation_audit(bt_root, body.validation_request_id)

    warnings = _validation_warnings_to_strings(body.validation_warnings)
    if not warnings and audit:
        warnings = _validation_warnings_to_strings(audit.get("warnings"))

    passed = body.validation_passed
    if passed is None and audit is not None:
        passed = bool(audit.get("pass"))

    report["data_validation"] = {
        "request_id": body.validation_request_id,
        "passed": passed,
        "warnings": warnings,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_path = out_dir / "report_summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
            audit = summary.get("audit", {})
            audit["validation"] = {
                "request_id": body.validation_request_id,
                "passed": passed,
                "warnings": warnings,
            }
            summary["audit"] = audit
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    conclusions_path = out_dir / "conclusions_10.json"
    if conclusions_path.exists():
        try:
            conclusions = json.loads(conclusions_path.read_text(encoding="utf-8-sig"))
            items = conclusions.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if int(item.get("id", -1)) != 1:
                        continue
                    if passed is False:
                        item["status"] = "FAIL"
                        item["value"] = "passed=false"
                        item["detail"] = f"validation_warnings={len(warnings)}"
                    elif passed is True:
                        item["status"] = "WARN" if len(warnings) > 0 else "PASS"
                        item["value"] = "passed=true"
                        item["detail"] = f"validation_warnings={len(warnings)}"
                    else:
                        item["status"] = "PASS"
                        item["value"] = "not_provided"
                        item["detail"] = "未绑定 validate 结果（passed=null）"
                    break
            conclusions_path.write_text(json.dumps(conclusions, ensure_ascii=False, indent=2), encoding="utf-8")
            txt_path = out_dir / "conclusions_10.txt"
            if txt_path.exists():
                lines = [f"run_id: {conclusions.get('run_id', '')}"]
                for item in items:
                    lines.append(
                        f"{item.get('id')}. [{item.get('status')}] {item.get('title')} | {item.get('value')} | {item.get('detail')}"
                    )
                nxt = conclusions.get("suggested_next_change", {})
                lines.append(f"next: {nxt.get('code', '')} | {nxt.get('reason', '')}")
                txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass


def _parse_symbols_from_query(request: Request) -> list[str]:
    vals = request.query_params.getlist("symbols")
    if not vals:
        return []
    if len(vals) == 1:
        return [x.strip() for x in vals[0].split(",") if x.strip()]
    out: list[str] = []
    for v in vals:
        out.extend([x.strip() for x in v.split(",") if x.strip()])
    return out


def _read_result(out_dir: Path) -> dict[str, Any]:
    report_path = out_dir / "report.json"
    summary_path = out_dir / "report_summary.json"
    trades_path = out_dir / "trades.csv"
    orders_path = out_dir / "orders.csv"
    equity_path = out_dir / "equity_curve.csv"
    warnings_path = out_dir / "warnings.json"
    conclusions_path = out_dir / "conclusions_10.json"
    benchmark_curve_path = out_dir / "benchmark_curve.csv"
    trade_pnl_path = out_dir / "trade_pnl.csv"
    signals_path = out_dir / "signals.csv"
    if not report_path.exists():
        raise HTTPException(status_code=500, detail=f"missing report file: {report_path}")

    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    report_summary: dict[str, Any] | None = None
    if summary_path.exists():
        try:
            report_summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
        except Exception:
            report_summary = None

    conclusions_10: dict[str, Any] | None = None
    if conclusions_path.exists():
        try:
            conclusions_10 = json.loads(conclusions_path.read_text(encoding="utf-8-sig"))
        except Exception:
            conclusions_10 = None

    benchmark_curve_preview: list[dict[str, Any]] = []
    if benchmark_curve_path.exists():
        try:
            cdf = pd.read_csv(benchmark_curve_path).head(2000)
            benchmark_curve_preview = cdf.to_dict(orient="records")
        except EmptyDataError:
            benchmark_curve_preview = []

    trade_concentration = {}
    if report_summary and isinstance(report_summary.get("trade_concentration"), dict):
        trade_concentration = report_summary.get("trade_concentration") or {}
    trades_preview: list[dict[str, Any]] = []
    if trades_path.exists():
        try:
            tdf = pd.read_csv(trades_path).head(200)
            trades_preview = tdf.to_dict(orient="records")
        except EmptyDataError:
            trades_preview = []

    return {
        "report": report,
        "summary": report.get("summary", {}),
        "report_summary": report_summary,
        "conclusions_10": conclusions_10 or (report_summary or {}).get("conclusions_10"),
        "chart": (report_summary or {}).get("chart", {}),
        "trade_concentration": trade_concentration,
        "benchmark_curve_preview": benchmark_curve_preview,
        "cost_breakdown": report.get("cost_breakdown", {}),
        "audit_rules": report.get("audit_rules", {}),
        "trades_preview": trades_preview,
        "files": {
            "report": str(report_path),
            "report_summary": str(summary_path),
            "trades": str(trades_path),
            "orders": str(orders_path),
            "equity": str(equity_path),
            "warnings": str(warnings_path),
            "conclusions_10": str(conclusions_path),
            "benchmark_curve": str(benchmark_curve_path),
            "trade_pnl": str(trade_pnl_path),
            "signals": str(signals_path),
        },
    }


def _sanitize_for_json(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, tuple):
        return [_sanitize_for_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_json(v) for v in value]
    return value


def _create_job(job_type: str, eta_seconds: int = 300) -> str:
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    now = time.time()
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "queued",
            "message": "绛夊緟鎵ц",
            "progress_pct": 0,
            "created_at_ts": now,
            "updated_at_ts": now,
            "started_at_ts": None,
            "finished_at_ts": None,
            "eta_seconds": int(max(30, eta_seconds)),
            "result": None,
            "error": None,
        }
        _persist_job(_JOBS[job_id])
    return job_id


def _update_job(job_id: str, **kwargs: Any) -> None:
    now = time.time()
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update(kwargs)
        job["updated_at_ts"] = now
        if kwargs.get("status") == "running" and job.get("started_at_ts") is None:
            job["started_at_ts"] = now
        if kwargs.get("status") in {"success", "failed"}:
            job["finished_at_ts"] = now
        _persist_job(job)


def _is_cancel_requested(job_id: str) -> bool:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            return bool(job.get("cancel_requested"))
    disk_job = _load_job_from_disk(job_id)
    return bool(disk_job and disk_job.get("cancel_requested"))


def _mark_job_canceled(job_id: str, message: str = "任务已取消") -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            job = _load_job_from_disk(job_id)
            if job is None:
                return None
            _JOBS[job_id] = job
        status = str(job.get("status") or "")
        if status in {"success", "failed", "canceled"}:
            return job
        now = time.time()
        job["cancel_requested"] = True
        job["status"] = "canceled"
        job["message"] = message
        job["finished_at_ts"] = now
        job["updated_at_ts"] = now
        _persist_job(job)
        return job


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "queued")
    progress = int(job.get("progress_pct") or 0)
    if status == "success":
        progress = 100
    if status == "canceled":
        progress = min(progress, 99)
    return {
        "job_id": job.get("job_id"),
        "job_type": job.get("job_type"),
        "status": status,
        "message": job.get("message"),
        "progress_pct": progress,
        "created_at": datetime.fromtimestamp(float(job.get("created_at_ts") or time.time())).isoformat(),
        "updated_at": datetime.fromtimestamp(float(job.get("updated_at_ts") or time.time())).isoformat(),
        "result": job.get("result"),
        "error": job.get("error"),
    }


def _write_experiment_click_report(bt_root: Path, result: dict[str, Any]) -> Path:
    report_dir = bt_root / "results" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    exp_id = str(result.get("exp_id", "exp"))
    preset = str(result.get("preset", "unknown"))
    out = report_dir / f"{stamp}_{exp_id}_{preset}.md"

    rows_raw = list(result.get("rows") or [])
    rows: list[dict[str, Any]] = []
    for rr in rows_raw:
        if isinstance(rr, dict):
            rows.append(rr)
        elif isinstance(rr, str):
            text = rr.strip()
            if text.startswith("@{") and text.endswith("}"):
                body = text[2:-1]
                parsed: dict[str, Any] = {}
                for part in body.split(";"):
                    if "=" not in part:
                        continue
                    k, v = part.split("=", 1)
                    parsed[str(k).strip()] = str(v).strip()
                rows.append(parsed)
            else:
                rows.append({"group_id": text, "status": "FAILED", "error_message": "ROW_PARSE_FAILED"})
        else:
            rows.append({"group_id": str(rr), "status": "FAILED", "error_message": "ROW_TYPE_INVALID"})
    lines: list[str] = [
        f"# 实验总结：{preset}",
        "",
        "## 1) 基本信息",
        f"- exp_id：{exp_id}",
        f"- 结果目录：{result.get('results_dir', '-')}",
        f"- 分组数量：{len(rows)}",
        "",
        "## 2) 关键数据",
    ]
    if not rows:
        lines.append("- 本次没有可展示的分组结果。")
    else:
        for row in rows[:8]:
            gid = str(row.get("group_id", "-"))
            status = str(row.get("status", "-"))
            ret = row.get("total_return")
            mdd = row.get("max_drawdown")
            sharpe = row.get("sharpe")
            ret_s = "-" if ret is None else f"{float(ret) * 100:.2f}%"
            mdd_s = "-" if mdd is None else f"{float(mdd) * 100:.2f}%"
            sharpe_s = "-" if sharpe is None else f"{float(sharpe):.3f}"
            lines.append(f"- {gid} | 状态：{status} | 收益：{ret_s} | 回撤：{mdd_s} | 夏普：{sharpe_s}")

    failed = [r for r in rows if str(r.get("status", "")).upper() == "FAILED"]
    lines.extend(
        [
            "",
            "## 3) 结论建议",
            f"- 成功分组：{len(rows) - len(failed)}",
            f"- 失败分组：{len(failed)}",
        ]
    )
    if failed:
        lines.append("- 建议先排查失败分组报错，再比较策略优劣。")
    else:
        lines.append("- 建议优先对比“收益、回撤、成交率”三项，再决定默认策略。")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _promote_single_md_report(bt_root: Path, result: dict[str, Any]) -> Path | None:
    report_dir = bt_root / "results" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    exp_id = str(result.get("exp_id", "exp"))
    preset = str(result.get("preset", "unknown"))

    # 1) explicit single report path
    single = str(result.get("single_report_path") or "").strip()
    if single:
        p = Path(single)
        if p.exists():
            out = report_dir / f"{stamp}_{exp_id}_{preset}.md"
            out.write_text(p.read_text(encoding="utf-8-sig"), encoding="utf-8")
            try:
                if p.resolve() != out.resolve():
                    p.unlink(missing_ok=True)
            except Exception:
                pass
            result["single_report_path"] = str(out)
            return out

    # 2) legacy md in experiment dir -> move out and clean up
    exp_dir = Path(str(result.get("results_dir") or ""))
    if exp_dir.exists():
        md_files = sorted(exp_dir.glob("*.md"))
        if md_files:
            latest = md_files[-1]
            out = report_dir / f"{stamp}_{exp_id}_{preset}.md"
            out.write_text(latest.read_text(encoding="utf-8-sig"), encoding="utf-8")
            for f in md_files:
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
            result["single_report_path"] = str(out)
            return out
    return None


@router.post("/api/backtest_v2/run")
def run_backtest_v2(body: BacktestV2RunRequest):
    raise HTTPException(
        status_code=410,
        detail=(
            "legacy /api/backtest_v2/run path is removed. "
            "Please use /api/backtest_v2/experiment/run_async with VECTOR_FAST_STRICT presets."
        ),
    )


def _run_backtest_v2_sync(body: BacktestV2RunRequest) -> dict[str, Any]:
    bt_root = _bt_root()
    if not bt_root.exists():
        raise HTTPException(status_code=500, detail=f"backtest_V2 root not found: {bt_root}")

    if not body.force_run:
        if not body.validation_request_id:
            raise HTTPException(status_code=409, detail="validation required: missing validation_request_id")
        audit = _read_validation_audit(bt_root, body.validation_request_id)
        if not audit:
            raise HTTPException(status_code=409, detail=f"validation required: audit not found for {body.validation_request_id}")
        if not bool(audit.get("pass")):
            raise HTTPException(status_code=409, detail="validation failed: run blocked unless force_run=true")

    cfg_path, run_id = _build_runtime_config(bt_root, body)
    try:
        out_dir = _run_backtest_v2(bt_root, cfg_path, run_id)
        _write_data_validation_section(out_dir, body, bt_root)
        payload = _sanitize_for_json(_read_result(out_dir))
        payload["run_id"] = run_id
        return payload
    finally:
        try:
            cfg_path.unlink(missing_ok=True)
        except Exception:
            pass


def _run_backtest_v2_job(job_id: str, body: BacktestV2RunRequest) -> None:
    try:
        if _is_cancel_requested(job_id):
            _update_job(job_id, status="canceled", message="任务已取消")
            return
        _update_job(job_id, status="running", progress_pct=5, message="校验参数与验收状态")
        payload = _run_backtest_v2_sync(body)
        if _is_cancel_requested(job_id):
            _update_job(job_id, status="canceled", message="任务已取消")
            return
        _update_job(job_id, status="running", progress_pct=90, message="整理结果与报告")
        if _is_cancel_requested(job_id):
            _update_job(job_id, status="canceled", message="任务已取消")
            return
        _update_job(job_id, status="success", progress_pct=100, message="回测完成", result=payload)
    except Exception as exc:
        msg = str(exc)
        if isinstance(exc, HTTPException):
            msg = str(exc.detail)
        if _is_cancel_requested(job_id):
            _update_job(job_id, status="canceled", message="任务已取消")
            return
        _update_job(job_id, status="failed", message="回测失败", error=msg)


@router.post("/api/backtest_v2/run_async")
def run_backtest_v2_async(body: BacktestV2RunRequest):
    raise HTTPException(
        status_code=410,
        detail=(
            "legacy /api/backtest_v2/run_async path is removed. "
            "Please use /api/backtest_v2/experiment/run_async with VECTOR_FAST_STRICT presets."
        ),
    )


@router.get("/api/backtest_v2/results/{run_id}")
def get_backtest_v2_result(run_id: str):
    bt_root = _bt_root()
    out_dir = bt_root / "results" / run_id
    if not out_dir.exists():
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    payload = _sanitize_for_json(_read_result(out_dir))
    payload["run_id"] = run_id
    return payload


@router.get("/api/backtest_v2/validate_data")
def validate_backtest_v2_data(
    request: Request,
    start: str = Query(...),
    end: str = Query(...),
    benchmark: str = Query("000300.SH"),
    audit_dump: bool = Query(True),
    strict_suspended: bool = Query(False),
    allow_cn_index_fallback: bool = Query(False),
):
    symbols = _parse_symbols_from_query(request)
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols is required")
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except Exception:
        raise HTTPException(status_code=400, detail="start/end must be YYYY-MM-DD")
    if start_d > end_d:
        raise HTTPException(status_code=400, detail="start must be <= end")

    bt_root = _bt_root()
    if not bt_root.exists():
        raise HTTPException(status_code=500, detail=f"backtest_V2 root not found: {bt_root}")

    if str(bt_root) not in sys.path:
        sys.path.insert(0, str(bt_root))
    from backtest_v2.data.validate_pg import PGConnConfig, validate_data  # type: ignore

    cfg = PGConnConfig(
        host=os.environ.get("PGHOST", os.environ.get("PAMS_PG_HOST", "pams-postgres")),
        port=int(os.environ.get("PGPORT", os.environ.get("PAMS_PG_PORT", "5432"))),
        dbname=os.environ.get("PGDATABASE", os.environ.get("PAMS_PG_DBNAME", "pams")),
        user=os.environ.get("PGUSER", os.environ.get("PAMS_PG_USER", "pams")),
        password=os.environ.get("PGPASSWORD", os.environ.get("PAMS_PG_PASSWORD", "")),
        statement_timeout_ms=60000,
    )
    result = validate_data(
        pg=cfg,
        symbols=symbols,
        start=start_d,
        end=end_d,
        benchmark=benchmark,
        audit_dump=audit_dump,
        strict_suspended=strict_suspended,
        allow_cn_index_fallback=allow_cn_index_fallback,
        results_root=bt_root / "results",
    )
    if isinstance(result, dict):
        result.setdefault("request_id", f"val_{uuid.uuid4().hex[:10]}")
        result.setdefault("results_root", str((bt_root / "results").resolve()))
    return _sanitize_for_json(result)


@router.post("/api/backtest_v2/experiment/run")
def run_backtest_v2_experiment(body: BacktestV2ExperimentRequest):
    return _run_backtest_v2_experiment_sync(body)


def _run_backtest_v2_experiment_sync(
    body: BacktestV2ExperimentRequest,
    progress_hook: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    bt_root = _bt_root()
    if not bt_root.exists():
        raise HTTPException(status_code=500, detail=f"backtest_V2 root not found: {bt_root}")

    if str(bt_root) not in sys.path:
        sys.path.insert(0, str(bt_root))
    try:
        from backtest_v2.tools.experiment_runner import run_experiment  # type: ignore
        from backtest_v2.tools.bundles import run_bundle  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to import experiment tools: {exc}")

    cfg_rel = body.config_path.strip() if (body.config_path or "").strip() else "configs/pams_cn_pg.json"
    cfg_path = (bt_root / cfg_rel).resolve()
    if not cfg_path.exists():
        raise HTTPException(status_code=400, detail=f"config not found: {cfg_path}")
    try:
        base_cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid config json: {cfg_path} ({exc})")

    overrides: dict[str, Any] = {}
    if body.symbols:
        overrides["symbols"] = list(body.symbols)
    if body.start_date:
        overrides["start_date"] = body.start_date
    if body.end_date:
        overrides["end_date"] = body.end_date
    if body.benchmark:
        overrides["benchmark"] = body.benchmark
    if body.compare_symbol:
        overrides["compare_symbol"] = body.compare_symbol
    if body.initial_capital is not None:
        overrides["initial_capital"] = float(body.initial_capital)
    if body.max_positions is not None:
        overrides["max_positions"] = int(body.max_positions)
    if body.base_order_value is not None:
        overrides["base_order_value"] = float(body.base_order_value)
    if body.univote_lot_mode:
        overrides["univote_lot_mode"] = str(body.univote_lot_mode)
    if body.lot_size is not None:
        overrides["lot_size"] = int(body.lot_size)
    if body.universe_limit is not None:
        overrides["universe_limit"] = int(body.universe_limit)
    if body.fast_mode is not None:
        overrides["fast_mode"] = bool(body.fast_mode)

    # Keep DB credentials aligned with /run path so in-process runner can read PG.
    env_backup = {
        "PAMS_PG_HOST": os.environ.get("PAMS_PG_HOST"),
        "PAMS_PG_PORT": os.environ.get("PAMS_PG_PORT"),
        "PAMS_PG_DBNAME": os.environ.get("PAMS_PG_DBNAME"),
        "PAMS_PG_USER": os.environ.get("PAMS_PG_USER"),
        "PAMS_PG_PASSWORD": os.environ.get("PAMS_PG_PASSWORD"),
    }
    os.environ["PAMS_PG_HOST"] = os.environ.get("PGHOST", env_backup["PAMS_PG_HOST"] or "pams-postgres")
    os.environ["PAMS_PG_PORT"] = os.environ.get("PGPORT", env_backup["PAMS_PG_PORT"] or "5432")
    os.environ["PAMS_PG_DBNAME"] = os.environ.get("PGDATABASE", env_backup["PAMS_PG_DBNAME"] or "pams")
    os.environ["PAMS_PG_USER"] = os.environ.get("PGUSER", env_backup["PAMS_PG_USER"] or "pams")
    os.environ["PAMS_PG_PASSWORD"] = os.environ.get("PGPASSWORD", env_backup["PAMS_PG_PASSWORD"] or "")

    preset_key = str(body.preset or "").strip().upper()
    try:
        if preset_key == "ATR_VALIDATE_BUNDLE_V1":
            bundle_result = run_bundle("ATR_VALIDATE_BUNDLE_V1", str(cfg_path), overrides, progress_hook=progress_hook)
            # Keep response compatible with existing matrix renderer.
            step1 = bundle_result.get("step1", {}) if isinstance(bundle_result, dict) else {}
            result = {
                "bundle": "ATR_VALIDATE_BUNDLE_V1",
                "bundle_dir": bundle_result.get("bundle_dir"),
                "preset": body.preset,
                "exp_id": str(step1.get("exp_id") or "bundle"),
                "results_dir": str(step1.get("results_dir") or bundle_result.get("bundle_dir") or ""),
                "rows": list(step1.get("rows") or []),
                "matrix_head": list(step1.get("matrix_head") or []),
                "runs": list(step1.get("runs") or []),
                "step1": step1,
                "step2": bundle_result.get("step2"),
                "step3": bundle_result.get("step3"),
            }
        else:
            result = run_experiment(str(cfg_path), body.preset, overrides, progress_hook=progress_hook)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"experiment run failed: {exc}")
    finally:
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    try:
        md = _promote_single_md_report(bt_root, result)
        if md is None:
            md = _write_experiment_click_report(bt_root, result)
        result["click_report_md"] = str(md)
    except Exception:
        pass
    try:
        _attach_run_profile_summary(result)
    except Exception:
        pass
    try:
        result = stabilize_experiment_result(
            bt_root=bt_root,
            result=result if isinstance(result, dict) else {},
            base_cfg=base_cfg,
            overrides=overrides,
            run_profile_summary=result.get("run_profile_summary") if isinstance(result, dict) else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"artifact stabilization failed: {exc}")
    result.setdefault("exp_id", "")
    result.setdefault("results_dir", "")
    result.setdefault("rows", [])
    result.setdefault("matrix_head", [])
    result.setdefault("runs", [])
    return _sanitize_for_json(result)


def _attach_run_profile_summary(result: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        return
    results_dir_raw = str(result.get("results_dir") or "").strip()
    if not results_dir_raw:
        return
    rp = Path(results_dir_raw) / "run_profile.json"
    if not rp.exists():
        return
    try:
        obj = json.loads(rp.read_text(encoding="utf-8-sig"))
    except Exception:
        return
    stages = obj.get("stage_seconds") if isinstance(obj.get("stage_seconds"), dict) else {}
    summary: dict[str, Any] = {
        "run_profile_path": str(rp),
        "total_wall_seconds": float(obj.get("total_wall_seconds", 0.0) or 0.0),
        "stage_seconds": {str(k): float(v or 0.0) for k, v in stages.items()},
        "symbols_count": int(obj.get("symbols_count", 0) or 0),
        "trading_days": int(obj.get("trading_days", 0) or 0),
        "variant_count": int(obj.get("variant_count", 0) or 0),
        "row_count": int(obj.get("row_count", 0) or 0),
        "rows_per_second": float(obj.get("rows_per_second", 0.0) or 0.0),
        "bottleneck_stage": str(obj.get("bottleneck_stage") or ""),
        "bottleneck_label": str(obj.get("bottleneck_label") or "BOTTLENECK_UNKNOWN"),
    }
    perf = obj.get("perf") if isinstance(obj.get("perf"), dict) else {}
    if perf:
        summary["perf"] = {
            "db_load_seconds": float(perf.get("db_load_seconds", 0.0) or 0.0),
            "precompute_seconds": float(perf.get("precompute_seconds", 0.0) or 0.0),
            "simulation_seconds": float(perf.get("simulation_seconds", 0.0) or 0.0),
            "io_write_seconds": float(perf.get("io_write_seconds", 0.0) or 0.0),
            "db_queries": int(perf.get("db_queries", 0) or 0),
            "cache_hit_rate": float(perf.get("cache_hit_rate", 0.0) or 0.0),
        }
    tips: list[str] = []
    b = summary["bottleneck_label"]
    if b == "BOTTLENECK_DATA_LOAD":
        tips.append("数据加载是主要瓶颈：建议减少 universe_limit 或缩短回测区间。")
    elif b == "BOTTLENECK_SIMULATION":
        tips.append("仿真执行是主要瓶颈：建议启用 fast_mode 并关闭非必要明细写盘。")
    elif b == "BOTTLENECK_PRECOMPUTE":
        tips.append("预计算是主要瓶颈：建议缩短 lookback 或减少因子维度。")
    elif b == "BOTTLENECK_IO":
        tips.append("写盘是主要瓶颈：建议关闭 trades/candidates 明细输出。")
    if float(summary.get("rows_per_second", 0.0) or 0.0) <= 0.0:
        tips.append("当前 rows_per_second=0，建议检查实验是否无有效分组输出。")
    summary["optimization_tips"] = tips
    result["run_profile_summary"] = summary


def _run_backtest_v2_experiment_job(job_id: str, body: BacktestV2ExperimentRequest) -> None:
    try:
        preset = str(body.preset or "G1-G4")
        if _is_cancel_requested(job_id):
            _update_job(job_id, status="canceled", message="实验已取消")
            return
        _update_job(job_id, status="running", progress_pct=3, message=f"开始实验：{preset}")

        def _progress_cb(pct: int, msg: str) -> None:
            if _is_cancel_requested(job_id):
                return
            _update_job(job_id, status="running", progress_pct=int(max(0, min(99, pct))), message=str(msg))

        payload = _run_backtest_v2_experiment_sync(body, progress_hook=_progress_cb)
        if _is_cancel_requested(job_id):
            _update_job(job_id, status="canceled", message="实验已取消")
            return
        _update_job(job_id, status="running", progress_pct=95, message="汇总矩阵与报告")
        if _is_cancel_requested(job_id):
            _update_job(job_id, status="canceled", message="实验已取消")
            return
        _update_job(job_id, status="success", progress_pct=100, message="实验完成", result=payload)
    except Exception as exc:
        msg = str(exc)
        if isinstance(exc, HTTPException):
            msg = str(exc.detail)
        if _is_cancel_requested(job_id):
            _update_job(job_id, status="canceled", message="实验已取消")
            return
        _update_job(job_id, status="failed", message="实验失败", error=msg)


@router.post("/api/backtest_v2/experiment/run_async")
def run_backtest_v2_experiment_async(body: BacktestV2ExperimentRequest):
    preset_key = str(body.preset or "").strip().upper()
    eta = 480
    if preset_key in {"G1-G4", "REGIME_BUFFER_2X2", "ATR_RISK_2X2", "ATR_PORTFOLIO_VALIDATE_V1", "PORTFOLIO_FILL_VALIDATE_V1", "TREND_FILTER_VALIDATE_V1", "TREND_TIER_VALIDATE_V1", "TREND_ACCEL_BRAKE_VALIDATE_V1", "TREND_BRAKE_CONFIRM_VALIDATE_V1", "STRENGTH_RANK_VALIDATE_V1", "FULL_RANGE_VALIDATE_V1", "CANDIDATE_DIAG_VALIDATE_V1", "SLOT_DEBUG_VALIDATE_V1", "MAX_POSITION_EXPAND_VALIDATE_V1", "STRONG_ONLY_VALIDATE_V1", "MULTIFACTOR_RANKING_VALIDATE_V1", "MULTIFACTOR_RANKING_WALKFORWARD_V1", "MF_RANK_REGIME_GATE_VALIDATE_V1", "MULTIFACTOR_BALANCED_V1", "MULTIFACTOR_FUSION_COMPARE_V1", "MULTIFACTOR_INCREMENTAL_V1", "MULTIFACTOR_INCREMENTAL_V1_6_1A", "EXPERIMENT_6_1A", "6.1A", "MULTIFACTOR_INCREMENTAL_V1_6_1B", "EXPERIMENT_6_1B", "6.1B", "MULTIFACTOR_INCREMENTAL_V1_6_1_LIMIT", "MULTIFACTOR_INCREMENTAL_V1_6_4", "EXPERIMENT_6_4", "6.4", "EXPERIMENT_6_1_LIMIT", "6.1L"}:
        eta = 240
    elif preset_key in {"STRENGTH_RANK_VALIDATE_V2", "STRENGTH_RANK_VALIDATE_V2A", "STRENGTH_RANK_VALIDATE_V2B", "ENGINE_PERF_COMPARE_V1", "ENGINE_PERF_COMPARE_V2"}:
        eta = 180 if bool(body.fast_mode) else 360
    elif preset_key in {"ATR_PARAM_SCAN_V1"}:
        eta = 1200
    elif preset_key in {"ATR_PARAM_VALIDATE_UNIVOTE", "ATR_PARAM_VALIDATE_UNIVOTE_V2"}:
        eta = 900
    job_id = _create_job("experiment", eta_seconds=eta)
    th = threading.Thread(target=_run_backtest_v2_experiment_job, args=(job_id, body), daemon=True)
    th.start()
    return {"job_id": job_id, "status": "queued"}


@router.get("/api/backtest_v2/jobs/{job_id}")
def get_backtest_v2_job(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        disk_job = _load_job_from_disk(job_id)
        if not disk_job:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        payload = _job_payload(dict(disk_job))
    else:
        payload = _job_payload(dict(job))
    return _sanitize_for_json(payload)


@router.post("/api/backtest_v2/jobs/{job_id}/cancel")
def cancel_backtest_v2_job(job_id: str):
    job = _mark_job_canceled(job_id, message="用户取消")
    if not job:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return _sanitize_for_json(_job_payload(dict(job)))
