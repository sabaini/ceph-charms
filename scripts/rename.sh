#!/bin/bash
set -euo pipefail

charm_dir="${1:-.}"
cd "${charm_dir}"

if [[ -f osci.yaml ]] && grep -q "charm_build_name" osci.yaml; then
    charm=$(awk '/charm_build_name/ {print $2; exit}' osci.yaml)
elif [[ -f metadata.yaml ]] && grep -q "^name" metadata.yaml; then
    charm=$(awk '/^name/ {print $2; exit}' metadata.yaml)
else
    echo "Unable to determine charm name from osci.yaml or metadata.yaml" >&2
    exit 1
fi
artifacts=("${charm}"_*.charm)

echo "Copying built charm artifacts for ${charm}"
echo -n "pwd: "
pwd
ls -al

if [[ ${#artifacts[@]} -eq 0 || ! -e "${artifacts[0]}" ]]; then
    echo "No charm artifacts found for ${charm}" >&2
    exit 1
fi

rm -f "${charm}.charm" "../${charm}.charm"
cp "${artifacts[@]}" ../

# Keep the historical generic artifact name for tooling that still expects it.
# A specific platform can be selected with CHARMCRAFT_GENERIC_PLATFORM or
# TEST_CHARM_PLATFORM (for example: ubuntu-26.04-amd64).  Otherwise prefer the
# 26.04 build because that matches the current test/default platform.
generic_platform="${CHARMCRAFT_GENERIC_PLATFORM:-${TEST_CHARM_PLATFORM:-}}"
default_artifact="${artifacts[0]}"
if [[ -n "${generic_platform}" ]]; then
    default_artifact="${charm}_${generic_platform}.charm"
    if [[ ! -e "${default_artifact}" ]]; then
        echo "Requested generic artifact ${default_artifact} was not built" >&2
        exit 1
    fi
elif [[ -e "${charm}_ubuntu-26.04-amd64.charm" ]]; then
    default_artifact="${charm}_ubuntu-26.04-amd64.charm"
fi
cp "${default_artifact}" "${charm}.charm"
cp "${charm}.charm" ../
