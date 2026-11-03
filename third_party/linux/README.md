# Vendored kernel uapi headers

Copies of Qualcomm kernel uapi headers from
[commaai/agnos-kernel-sdm845](https://github.com/commaai/agnos-kernel-sdm845)
(the kernel shipped on comma 3/3X and its clones), GPL-2.0. Identical to what
`make headers_install` would put in `/usr/include`: kernel-only `__user`
annotations have been stripped.

Why: the on-device scons build needs them, but some device rootfses (e.g. the
C3L clone's AGNOS 12.4) ship a stock linux-libc-dev without them:

- `linux/ion.h`, `linux/msm_ion.h` — `msgq_repo/msgq/visionipc/visionbuf_ion.cc`
- `media/msm_media_info.h` — `system/loggerd/encoder/v4l_encoder.cc`
- `media/msm_camsensor_sdk.h` — `system/camerad/sensors/*.cc`
- `media/cam_*.h` (the CAMX uapi: cam_defs, cam_icp, cam_isp, cam_isp_ife,
  cam_isp_vfe, cam_req_mgr, cam_sensor, cam_sync) —
  `system/camerad/cameras/spectra.h` and the qcom2 camera stack

`SConstruct` adds `include/` to `CPPPATH` only when the system headers are
missing (the tell is `/usr/include/linux/ion.h`), so official AGNOS devices
keep using their own copies. The layout mirrors a sysroot
(`include/linux/...`, `include/media/...`), so includes resolve as
`<linux/ion.h>` / `<media/msm_media_info.h>` like on AGNOS.

These are the legacy (pre-4.12) ION ABI (`ion_allocation_data.handle`,
`ION_IOC_SHARE`/`IMPORT`) that the msm-4.9 kernel on these devices speaks —
the post-4.12 uapi from mainline kernels is incompatible and must not be used
here.

The qcom msm_vidc control IDs that live inside otherwise-standard
`linux/v4l2-controls.h` are not vendored (shadowing a whole system header for
every file in the tree is fragile). Instead `system/loggerd/encoder/v4l_encoder.cc`
and `v4l_decoder.h` carry `#ifndef` fallbacks for the ones they use
(`V4L2_CID_MPEG_MSM_VIDC_BASE`, `V4L2_CID_MPEG_VIDC_VIDEO_*` and their enum
values, plus `V4L2_MPEG_VIDEO_H264_LEVEL_UNKNOWN`, a qcom-only member of the
mainline h264 level enum), with values taken from the same kernel source above.
On AGNOS the real `/usr/include` definitions win; on stock linux-libc-dev the
fallbacks kick in. Note the msm-4.9 offsets differ from the pre-4.9
`V4L2_CID_MPEG_BASE+100..` generation and must not be mixed up.
