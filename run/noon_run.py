#!/usr/bin/env python3
# SPDX-FileCopyrightText: Open Energy Transition gGmbH
#
# SPDX-License-Identifier: MIT
"""
Interactive runner for scenarios defined in config/scenarios.noon.yaml.

For each scenario, the script compares its overrides against the merged base
config (config.default.yaml + selected noon config) to decide how many
resources to reuse from the reference scenario:

  prepare_sector_network  — only StorageUnit/Store extendable_carriers changed.
                            *_elec.nc and snapshot_weightings are symlinked.
  add_electricity         — Generator, foresight, planning_horizons, or
                            load.scaling_factor changed. Topology files are
                            symlinked; electricity networks are not.
  full_rerun              — snapshots or atlite cutout changed. Nothing is
                            symlinked; every rule runs from scratch.

foresight and planning_horizons are only overridden in the Snakemake command
when the scenario value actually differs from the base config.

Usage (from repo root):
    python run/noon_run.py
"""

import os
import re
from pathlib import Path

import yaml

SCENARIOS_FILE = "config/scenarios.noon.yaml"
_DEFAULT_REFERENCE = "cy2021-base"  # pre-selected default reference scenario
TARGET = "solve_sector_networks"

# Topology networks safe to symlink regardless of restart level
_SAFE_NETWORK_RE = re.compile(
    r"^networks/(base|base_extended|base_s|base_s_[a-zA-Z0-9]+)\.nc$"
)
# Electricity networks safe to symlink when only sector/storage changes
_ELEC_NETWORK_RE = re.compile(r"^networks/base_s_[a-zA-Z0-9]+_elec[^/]*\.nc$")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins on conflicts)."""
    merged = {**base}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_merged_config(config_path: str) -> dict:
    """Return config.default.yaml deep-merged with the selected user config."""
    with open("config/config.default.yaml") as f:
        default = yaml.safe_load(f)
    with open(config_path) as f:
        user = yaml.safe_load(f)
    return _deep_merge(default, user)


# ---------------------------------------------------------------------------
# Restart-level classification
# ---------------------------------------------------------------------------


def get_restart_level(scenario_cfg: dict, base_cfg: dict) -> str:
    """
    Return the earliest rule that must re-run for this scenario.

    Returns one of: 'full_rerun', 'add_electricity', 'prepare_sector_network'.
    """
    # Snapshots or cutout change → all time-series profiles need rebuilding
    if "snapshots" in scenario_cfg or "atlite" in scenario_cfg:
        return "full_rerun"

    base_elec = base_cfg.get("electricity", {}).get("extendable_carriers", {})
    scen_elec = (scenario_cfg.get("electricity") or {}).get("extendable_carriers", {})

    # Changes that affect add_electricity output
    checks = [
        # Generator extendable carriers set p_nom_extendable on renewable generators
        sorted(scen_elec.get("Generator", base_elec.get("Generator", [])))
        != sorted(base_elec.get("Generator", [])),
        # Foresight affects which solve rules Snakemake includes
        (
            scenario_cfg.get("foresight") is not None
            and scenario_cfg["foresight"] != base_cfg.get("foresight", "overnight")
        ),
        # Planning horizons requires different cost files and sector resources
        (
            (scenario_cfg.get("scenario") or {}).get("planning_horizons") is not None
            and (scenario_cfg.get("scenario") or {}).get("planning_horizons")
            != base_cfg.get("scenario", {}).get("planning_horizons")
        ),
        # Load scaling factor enters add_electricity via attach_load
        (
            (scenario_cfg.get("load") or {}).get("scaling_factor") is not None
            and (scenario_cfg.get("load") or {}).get("scaling_factor")
            != base_cfg.get("load", {}).get("scaling_factor", 1.0)
        ),
    ]

    if any(checks):
        return "add_electricity"

    # Only StorageUnit/Store/sector changes: prepare_sector_network removes
    # and re-adds storage anyway, so *_elec.nc is reusable
    return "prepare_sector_network"


# ---------------------------------------------------------------------------
# Symlinking
# ---------------------------------------------------------------------------


def _must_rerun(rel: str, restart_level: str) -> bool:
    """True if this resource file should NOT be symlinked from the reference."""
    if rel.startswith("networks/") and rel.endswith(".nc"):
        if _SAFE_NETWORK_RE.match(rel):
            return False
        if restart_level == "prepare_sector_network" and _ELEC_NETWORK_RE.match(rel):
            return False
        return True
    if "snapshot_weightings_" in rel and rel.endswith(".csv"):
        return restart_level != "prepare_sector_network"
    return False


def _align_symlink_mtime(link: Path, src: Path) -> None:
    """Make symlink mtime match source mtime to keep DAG timestamps stable."""
    try:
        st = src.stat()
        os.utime(link, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
    except (FileNotFoundError, NotImplementedError, OSError):
        # Best-effort only: if this fails, symlinking still works.
        return


def symlink_resources(src: Path, dst: Path, restart_level: str) -> list[Path]:
    """Symlink reusable files from reference dir to target dir."""
    created = []
    for f in sorted(src.rglob("*")):
        if f.is_dir():
            continue
        rel = str(f.relative_to(src))
        if _must_rerun(rel, restart_level):
            continue
        link = dst / rel
        if link.is_symlink():
            # Existing links may have fresh link mtimes from a previous run.
            # Align them so Snakemake does not treat upstream inputs as newer.
            _align_symlink_mtime(link, f)
            continue
        if link.exists():
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(f.resolve())
        _align_symlink_mtime(link, f)
        created.append(link)
    return created


def cleanup_metadata(files: list[Path], base_config: str, cfg_args: str) -> None:
    """Remove stale Snakemake metadata for symlinked files (chunked)."""
    chunk = 200
    for i in range(0, len(files), chunk):
        batch = " ".join(str(p) for p in files[i : i + chunk])
        _run(
            f"snakemake --cleanup-metadata {batch}"
            f" --configfile {base_config} --config {cfg_args}"
        )


# ---------------------------------------------------------------------------
# Snakemake command helpers
# ---------------------------------------------------------------------------


def build_config_args(name: str, scenario_cfg: dict, base_cfg: dict) -> str:
    """
    Return the --config argument string for Snakemake.

    planning_horizons and foresight are only overridden when they differ from
    the base config (they must be set globally so Snakemake includes the right
    rules and uses the correct wildcards).
    """
    parts = [f"'run={{name: {name}}}'"]

    scen_ph = (scenario_cfg.get("scenario") or {}).get("planning_horizons")
    base_ph = base_cfg.get("scenario", {}).get("planning_horizons")
    if scen_ph and scen_ph != base_ph:
        parts.append(f"'scenario={{planning_horizons: {scen_ph}}}'")

    scen_foresight = scenario_cfg.get("foresight")
    base_foresight = base_cfg.get("foresight", "overnight")
    if scen_foresight and scen_foresight != base_foresight:
        parts.append(f"foresight={scen_foresight}")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def _run(cmd: str) -> None:
    print(f"  $ {cmd}")
    os.system(cmd)


def select_config() -> str:
    """Discover noon config files and let the user pick one."""
    configs = sorted(Path("config").glob("config.*noon*.yaml"))
    if not configs:
        raise SystemExit("No config files matching config/config.*noon*.yaml found.")
    if len(configs) == 1:
        print(f"  Using config: {configs[0]}")
        return str(configs[0])
    print("\nAvailable config files:")
    for i, c in enumerate(configs, 1):
        print(f"  {i}. {c.name}")
    while True:
        raw = input("> ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(configs):
            return str(configs[int(raw) - 1])
        match = [c for c in configs if c.name == raw or str(c) == raw]
        if match:
            return str(match[0])
        print("  Invalid choice, try again.")


def select_reference(names: list[str], resource_base: Path) -> str | None:
    """Let the user pick a reference scenario for resource symlinking."""
    default = _DEFAULT_REFERENCE if _DEFAULT_REFERENCE in names else None
    print("\nReference scenario for resource symlinking:")
    for i, n in enumerate(names, 1):
        exists = "✓" if (resource_base / n).is_dir() else " "
        marker = " (default)" if n == default else ""
        print(f"  {exists} {i}. {n}{marker}")
    print("  0. none (full pipeline for all scenarios)")
    hint = f" [{default}]" if default else ""
    while True:
        raw = input(f"Reference{hint}: ").strip()
        if not raw and default:
            return default
        if raw in ("0", "none"):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        if raw in names:
            return raw
        print("  Not found, try again.")


def select_scenarios(names: list[str]) -> list[str] | None:
    """Let the user pick one or more scenarios to run."""
    print("\nAvailable scenarios:")
    print("  0. all")
    for i, n in enumerate(names, 1):
        print(f"  {i}. {n}")
    print("  → single: 1  |  subset: 1,3,5  |  all: 0")
    while True:
        raw = input("> ").strip()
        if not raw:
            return None
        if raw in ("0", "all"):
            return list(names)
        result, ok = [], True
        for token in [t.strip() for t in raw.split(",")]:
            if token.isdigit() and 1 <= int(token) <= len(names):
                result.append(names[int(token) - 1])
            elif token in names:
                result.append(token)
            else:
                print(f"  Not found: '{token}'")
                ok = False
                break
        if ok and result:
            seen: set = set()
            return [x for x in result if not (x in seen or seen.add(x))]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if not Path("Snakefile").exists():
        raise SystemExit("Run this script from the repository root.")

    print("=" * 52)
    print("  Noon Energy Scenarios Runner")
    print("=" * 52)

    base_config = select_config()
    base_cfg = load_merged_config(base_config)
    prefix = base_cfg.get("run", {}).get("prefix", "")
    resource_base = Path("resources") / prefix if prefix else Path("resources")

    with open(SCENARIOS_FILE) as f:
        scenarios: dict = yaml.safe_load(f)
    names = list(scenarios.keys())

    reference = select_reference(names, resource_base)
    selected = select_scenarios(names)
    if not selected:
        print("Cancelled.")
        return

    raw_cores = input("\nCPUs (all or number) [all]: ").strip() or "all"
    cores = "-call" if raw_cores.lower() == "all" else f"-c{raw_cores}"
    want_dry = input("Dry-run before each scenario? [Y/n]: ").strip().lower() not in (
        "n",
        "no",
    )

    print(f"\nConfig    : {base_config}")
    print(f"Prefix    : {resource_base}")
    print(f"Reference : {reference or 'none'}")
    print(f"Selected  : {', '.join(selected)}")
    print(f"Cores     : {cores}  |  Dry-run: {want_dry}")
    print("=" * 52)

    ref_dir = resource_base / reference if reference else None

    for name in selected:
        print(f"\n{'─' * 45}")
        print(f"  Scenario: {name}")

        scenario_cfg = scenarios.get(name) or {}
        restart_level = get_restart_level(scenario_cfg, base_cfg)
        cfg_args = build_config_args(name, scenario_cfg, base_cfg)
        snk_cmd = (
            f"snakemake {cores} {TARGET} --configfile {base_config} --config {cfg_args}"
        )
        dst_dir = resource_base / name

        # Resource sharing
        if name == reference:
            print("  Reference → full pipeline.")
        elif restart_level == "full_rerun":
            print("  Snapshots/cutout changed → full rerun.")
        elif ref_dir is not None and ref_dir.is_dir():
            links = symlink_resources(ref_dir, dst_dir, restart_level)
            if links:
                rerun_from = restart_level.replace("_", " ")
                print(f"  Symlinked {len(links)} files (re-running from {rerun_from}).")
                cleanup_metadata(links, base_config, cfg_args)
        elif ref_dir is None:
            print("  No reference → full pipeline.")
        else:
            print("  Reference not yet built → full pipeline.")

        # Dry run
        if want_dry:
            _run(snk_cmd + " --dry-run")
            if input("\n  Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("  Skipped.")
                continue

        _run(snk_cmd)

        if name == reference and ref_dir is not None:
            ref_dir = resource_base / reference

    print("\n" + "=" * 52)
    print("  Done.")


if __name__ == "__main__":
    main()
