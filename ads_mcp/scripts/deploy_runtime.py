"""Cuts an immutable deployment checkout of the MCP at an exact revision.

The MCP must not be launched from a developer working tree. A working
tree changes under `git checkout`, so two sessions started minutes apart
can execute different code, and a session can bind to an unreviewed
branch without anyone noticing. That happened: see
ads_mcp/governance/provenance.py.

This creates a separate git worktree in DETACHED HEAD at one specific
commit. Detached is the point -- it follows no branch, so ordinary
development cannot move it. It then writes a RUNTIME_REVISION stamp so
the running process can state its own revision without shelling out to
git, and prints the launch configuration to use.

Usage:
    uv run -m ads_mcp.scripts.deploy_runtime --revision origin/lo-media-mutations
    uv run -m ads_mcp.scripts.deploy_runtime --revision <sha> --dest <path>

Nothing here touches Google Ads, and nothing here restarts a running MCP.
Deployment takes effect the next time a session spawns one.
"""

import argparse
import json
import pathlib
import subprocess
import sys

from ads_mcp.governance import provenance

DEFAULT_REVISION = "origin/lo-media-mutations"
DEFAULT_DEST_NAME = "google_ads_mcp_runtime"


def _run(args: list[str], cwd: pathlib.Path) -> str:
  """Runs a git command, raising with its stderr on failure."""
  result = subprocess.run(
      args, cwd=str(cwd), capture_output=True, text=True, check=False
  )
  if result.returncode != 0:
    raise SystemExit(
        f"command failed: {' '.join(args)}\n{result.stderr.strip()}"
    )
  return result.stdout.strip()


def _resolve_revision(source: pathlib.Path, revision: str) -> str:
  """Turns a ref into the exact 40-character commit it names."""
  resolved = _run(["git", "rev-parse", f"{revision}^{{commit}}"], source)
  if len(resolved) != 40:
    raise SystemExit(f"could not resolve {revision!r} to a commit")
  return resolved


def deploy(source: pathlib.Path, dest: pathlib.Path, revision: str) -> dict:
  """Creates or re-points the deployment checkout, then verifies it."""
  _run(["git", "fetch", "origin", "--quiet"], source)
  commit = _resolve_revision(source, revision)

  if (dest / ".git").exists():
    # Re-point an existing deployment. Detached again, deliberately.
    _run(["git", "checkout", "--detach", commit], dest)
  else:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["git", "worktree", "add", "--detach", str(dest), commit],
        source,
    )

  # Verify BEFORE stamping. Stamping an unverified tree would hand the
  # runtime a confident answer that happens to be wrong.
  deployed = _run(["git", "rev-parse", "HEAD"], dest)
  if deployed != commit:
    raise SystemExit(
        f"deployment verification failed: {dest} is at {deployed}, "
        f"expected {commit}"
    )
  dirty = _run(
      ["git", "status", "--porcelain", "--untracked-files=no"], dest
  )
  if dirty:
    raise SystemExit(
        f"deployment verification failed: tracked files under {dest} are "
        "modified. A deployment checkout must never be edited in place."
    )

  (dest / provenance.STAMP_FILENAME).write_text(commit + "\n", encoding="utf-8")

  return {"revision": commit, "dest": str(dest), "source": str(source)}


def launch_config(result: dict) -> dict:
  """The `~/.claude.json` block that pins a session to this deployment."""
  return {
      "google-ads": {
          "type": "stdio",
          "command": "uv",
          "args": [
              "run",
              "--directory",
              result["dest"],
              "-m",
              "ads_mcp.stdio",
          ],
          "env": {
              "GOOGLE_ADS_CREDENTIALS": "<path to google-ads.yaml>",
              "ADS_MCP_ENABLE_MUTATIONS": "true",
              "ADS_MCP_ENABLE_RAW_MUTATIONS": "false",
              provenance.PINNED_REVISION_VAR: result["revision"],
          },
      }
  }


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--revision", default=DEFAULT_REVISION)
  parser.add_argument("--dest", default=None)
  args = parser.parse_args(argv)

  source = pathlib.Path(__file__).resolve().parent.parent.parent
  dest = (
      pathlib.Path(args.dest).resolve()
      if args.dest
      else source.parent / DEFAULT_DEST_NAME
  )

  result = deploy(source, dest, args.revision)

  print(f"deployed {result['revision']} -> {result['dest']}", file=sys.stderr)
  print(
      "\nUpdate the google-ads MCP block in ~/.claude.json to:\n",
      file=sys.stderr,
  )
  print(json.dumps(launch_config(result), indent=2))
  print(
      "\nThen fully restart Claude Code. Running sessions keep the code "
      "they already imported; deployment takes effect on the next spawn.",
      file=sys.stderr,
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
