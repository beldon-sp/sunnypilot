# MAPD implementation by pfeiferj
https://github.com/pfeiferj/openpilot-mapd/releases/

## vendored binary is patched

`mapd` is rebuilt from the upstream v1.12.0 source with `mapd_parallel.patch` applied
(concurrent tile downloads; concurrency from the `OSMDownloadConcurrency` param, default 4,
clamped 1..16). Upstream's release binary downloads tiles strictly sequentially, which is the
dominant bottleneck for the multi-GB country downloads from CN networks.

Rebuild after a version bump:

    ./build.sh            # clone tag, apply patch, cross-compile linux/arm64
    shasum -a 256 mapd    # paste into MAPD_SHA256 in sunnypilot/mapd/mapd_installer.py

The patch bundles a live-network test (`go test -run TestDownloadBoundsParallel`).
