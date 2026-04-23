# WSL2 + CUDA JAX setup (RTX 3070, driver 576.52)

## Why

JAX dropped native-Windows CUDA support after 0.4.x. Getting GPU
acceleration requires running JAX inside WSL2 with Linux CUDA
userspace; the Windows NVIDIA driver exposes the GPU to WSL via
`/usr/lib/wsl/lib/libcuda.so.1` with no additional Linux driver
install needed.

Target: 420-trajectory cvar20 evaluation, currently 1.3-2.2 s on
CPU fp32, expected ~150-400 ms on 3070.

## Prerequisites (Windows host, admin required)

1. **Admin PowerShell** (required for WSL feature install):
   ```powershell
   # 1. Install WSL2 + default Ubuntu distro.  Triggers a reboot.
   wsl --install -d Ubuntu-24.04
   ```
   This enables the `Microsoft-Windows-Subsystem-Linux` and
   `VirtualMachinePlatform` features, installs the WSL2 kernel,
   and downloads Ubuntu 24.04.  Reboot when prompted.

2. After reboot, Ubuntu launches and prompts for a UNIX username +
   password.  Pick anything — this account is only used inside WSL.

3. Back in PowerShell, verify:
   ```powershell
   wsl --status      # should show WSL 2
   wsl -l -v         # Ubuntu-24.04, running, version 2
   ```

## Prerequisites (inside WSL Ubuntu)

From Windows PowerShell, drop into the distro:
```powershell
wsl -d Ubuntu-24.04
```

Then inside Ubuntu:
```bash
# Verify GPU is visible.  No Linux NVIDIA driver needed —
# /usr/lib/wsl/lib/ is injected by the Windows driver.
nvidia-smi
# Expected: "NVIDIA GeForce RTX 3070 ... Driver Version: 576.52"
```

## One-shot setup script

From inside WSL, at `/mnt/c/VSProjects/regenx-brake-assist-controller`:
```bash
bash scripts/setup_wsl_jax_cuda.sh
```

This creates a separate Linux venv at `.venv-linux/`, installs
`jax[cuda12]`, and runs a sanity check confirming
`jax.devices() -> [CudaDevice(id=0)]`.

## Running benchmarks inside WSL

```bash
source .venv-linux/bin/activate
python scripts/benchmark_jax_startup.py fp32
# Expect: ctor ~500-1500 ms, eval #1 ~300-800 ms, eval #2 ~100-300 ms
python scripts/benchmark_jax_cvar20.py
# Expect: 40-100x speedup vs numpy instead of 17x
```

## Maintenance

- `.venv/` (Windows) stays for numba, bench hardware scripts, tests.
- `.venv-linux/` is JAX-only.  `pip freeze` differs — don't cross them.
- `sim/output/` is shared via `/mnt/c/...` so sim_gallery HTML stays
  readable from Windows VS Code.
