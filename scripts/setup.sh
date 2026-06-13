#!/usr/bin/env bash
# Hydra portability bootstrap (POSIX / bash). Mirror of scripts/setup.ps1.
#
# Implements the AIAPP_BASE convention so the repo locates itself + siblings
# dynamically, with NO machine-specific absolute paths baked into source.
#
# Resolution order:
#   1. Per-repo env override (AIAPP_BASE).
#   2. Anchor-relative auto-detect: HYDRA_ROOT = parent of this scripts/ dir.
#   3. Siblings under AIAPP_BASE env, else dirname(HYDRA_ROOT).
#   4. If unresolved -> FAIL LOUD naming the env var. Never fall back to a
#      literal /c/AiAppDeployments.
#
# Actions (all idempotent):
#   - (Re)create the 5 squads/marketing-* symlinks into MarketBliss.
#   - Generate ~/.hydra/backends.json from scripts/backends.template.json,
#     substituting {{AIAPP_BASE}} and {{HYDRA_ROOT}}.

set -euo pipefail

c_cyan='\033[0;36m'; c_green='\033[0;32m'; c_yellow='\033[0;33m'; c_gray='\033[0;90m'; c_off='\033[0m'
section() { printf '\n'"${c_cyan}"'== %s =='"${c_off}"'\n' "$1"; }
ok()      { printf "${c_green}"'  [ok]   %s'"${c_off}"'\n' "$1"; }
skip()    { printf "${c_gray}"'  [skip] %s'"${c_off}"'\n' "$1"; }
warn()    { printf "${c_yellow}"'  [warn] %s'"${c_off}"'\n' "$1"; }
die()     { printf "${c_yellow}"'ERROR: %s'"${c_off}"'\n' "$1" >&2; exit 1; }

# --- (2) HYDRA_ROOT = parent of this scripts/ dir, from the script's own location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
HYDRA_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- (1)/(3) AIAPP_BASE = env override, else parent of HYDRA_ROOT.
if [ -n "${AIAPP_BASE:-}" ]; then
  AIAPP_BASE="$(cd "${AIAPP_BASE}" && pwd)"
else
  parent="$(cd "${HYDRA_ROOT}/.." && pwd)" || parent=""
  # --- (4) FAIL LOUD.
  [ -n "${parent}" ] || die "AIAPP_BASE is not set and could not be derived from HYDRA_ROOT ('${HYDRA_ROOT}'). Set the AIAPP_BASE environment variable to the directory containing all repos."
  AIAPP_BASE="${parent}"
fi

section "Resolved paths"
printf '  HYDRA_ROOT  = %s\n' "${HYDRA_ROOT}"
printf '  AIAPP_BASE  = %s\n' "${AIAPP_BASE}"

# --- Symlinks: squads/marketing-* -> <AIAPP_BASE>/MarketBliss/squads/marketing-*
section "Marketing squad symlinks"
marketing_names=(creative ops production research strategy)
squads_dir="${HYDRA_ROOT}/squads"
mb_squads_dir="${AIAPP_BASE}/MarketBliss/squads"

mkdir -p "${squads_dir}"

mb_present=0
[ -d "${mb_squads_dir}" ] && mb_present=1
if [ "${mb_present}" -eq 0 ]; then
  warn "MarketBliss not found at ${mb_squads_dir} -- skipping symlink creation."
fi

for name in "${marketing_names[@]}"; do
  link_name="marketing-${name}"
  link_path="${squads_dir}/${link_name}"
  target_path="${mb_squads_dir}/${link_name}"

  if [ "${mb_present}" -eq 0 ]; then
    skip "${link_name} (MarketBliss missing)"
    continue
  fi

  if [ -L "${link_path}" ]; then
    current="$(readlink "${link_path}")"
    # Resolve relative targets against the link's own dir for comparison.
    case "${current}" in
      /*) current_abs="${current}" ;;
      *)  current_abs="$(cd "${squads_dir}" && cd "$(dirname "${current}")" 2>/dev/null && pwd)/$(basename "${current}")" || current_abs="${current}" ;;
    esac
    if [ "${current_abs%/}" = "${target_path%/}" ] || [ "${current%/}" = "${target_path%/}" ]; then
      skip "${link_name} already linked correctly"
      continue
    fi
    rm -f "${link_path}"
  elif [ -e "${link_path}" ]; then
    # Exists but not a symlink (real dir/file) -- remove and recreate.
    rm -rf "${link_path}"
  fi

  if ln -s "${target_path}" "${link_path}" 2>/dev/null; then
    ok "${link_name} -> ${target_path}"
  else
    warn "Failed to create symlink '${link_name}'."
    warn "  Hint: ensure you have permission to create symlinks in ${squads_dir}."
  fi
done

# --- backends.json generation from template.
section "backends.json"
template_path="${SCRIPT_DIR}/backends.template.json"
[ -f "${template_path}" ] || die "Template not found: ${template_path}"

hydra_dir="${HOME}/.hydra"
backends_out="${hydra_dir}/backends.json"
mkdir -p "${hydra_dir}"

if [ -f "${backends_out}" ]; then
  cp -f "${backends_out}" "${backends_out}.bak"
  ok "backed up existing backends.json -> ${backends_out}.bak"
fi

# Substitute placeholders. Use a sed-safe delimiter (paths contain no '|').
sed -e "s|{{AIAPP_BASE}}|${AIAPP_BASE}|g" \
    -e "s|{{HYDRA_ROOT}}|${HYDRA_ROOT}|g" \
    "${template_path}" > "${backends_out}"
ok "wrote ${backends_out}"

# --- Summary.
section "Summary"
printf '  HYDRA_ROOT       : %s\n' "${HYDRA_ROOT}"
printf '  AIAPP_BASE       : %s\n' "${AIAPP_BASE}"
printf '  backends.json    : %s\n' "${backends_out}"
printf '\n'
printf "${c_cyan}"'  Export this so other repos + tools agree on the base:'"${c_off}"'\n'
printf '    export AIAPP_BASE="%s"   # add to ~/.bashrc or ~/.zshrc\n' "${AIAPP_BASE}"
printf '\n'
printf "${c_green}"'Done.'"${c_off}"'\n'
