import json, shutil, sys, time, datetime, dataclasses
from pathlib import Path
S = Path(sys.argv[1])
from quadratus.isolated_run import run_isolated
image = (S / "image-id.txt").read_text().strip()
started = time.monotonic(); began = datetime.datetime.now(datetime.timezone.utc).isoformat()
out = {"started_at": began, "image": image}
try:
    result = run_isolated(image=image, work=str(S / "work"), runtime=str(S / "runtime"),
                          command=["python", "/opt/quadratus/blind_trial.py"],
                          wall_seconds=3000, network=True, credentials=str(S / "seed"))
    out["result"] = dataclasses.asdict(result) if dataclasses.is_dataclass(result) else repr(result)
except BaseException as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"
finally:
    shutil.rmtree(S / "seed", ignore_errors=True)
    out["seed_removed"] = not (S / "seed").exists()
    out["elapsed_seconds"] = round(time.monotonic() - started, 1)
    out["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    (S / "outer-result.json").write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(json.dumps(out, indent=2, default=str))
