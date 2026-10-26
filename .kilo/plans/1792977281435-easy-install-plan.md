# Easy install plan (Linux install.sh + Windows setup.exe guidance)

## Goal
Pengguna awam dapat terinstal dalam satu langkah tanpa kebingungan:
- Linux: satu perintah `curl -fsSL https://decomp.ndelic.com/install.sh | bash` berhasil dari keadaan fresh-install distro umum, dengan setiap kegagalan menjelaskan cara memperbaikinya.
- Windows: tanpa menyentuh `build.ps1`; pengguna cukup unduh + buka `setup.exe` dari releases, dengan panduan README/situs yang sama jelasnya.
- Perubahan `install.sh` minimal: tidak mengubah arsitektur (uv tool install → `decomp setup` → `decomp shortcut` → start), hanya pengerasan pesan/cek.

## Non-goals (explicitly out of scope)
- Menulis logika install native Windows di bash; `install.sh` tetap menolak non-Linux (baris 64–66), hanya pesannya diperjelas.
- Mengubah `packaging/windows/build.ps1`, `packaging/linux/build.sh`, `src/decompnet/app/updates.py`, `src/decompnet/app/cli.py`, `src/decompnet/toolchains.py` kecuali terbukti perlu oleh satu failure mode.
- Dukungan macOS / ARM: tetap pesan penolakan yang jelas (alasan: compiler game x86).
- Menambah dependensi runtime baru.

## Context (verified in code)
- `site/public/install.sh` (179 baris): installer Linux. Header baris 2 menyatakan hanya Linux. Cek OS (64–66), cek arch x86_64 (69–71), butuh `curl` (72), install `uv` bila absen (107–114, `ask` dulu), `uv tool install --force --python 3.12 SPEC` (129), `decomp install-info` (133), cek GTK/WebKitGTK 4.1 per-distro dengan perintah siap salin (137–150), `decomp setup` unduh compiler (153), `decomp stop` versi lama (156), tawarkan hapus paket .deb/.rpm lama (159–166), `decomp shortcut` (168), start bila ada DISPLAY/Wayland (172–178). Channel update diingat via install-info (updates.py `installed()`/`record()`; `_check_release` mem-parse `^VERSION="..."` dari install.sh di main — maka VERSION harus selalu sinkron).
- `packaging/release.sh`: bump versi di 4 tempat (pyproject, `__init__.py`, install.sh, uv.lock). `release.yml`: job linux bangun .deb/.rpm/AppImage; job windows (`packaging/windows/build.ps1`) bangun `decompdecomp-<ver>-windows-x64-setup.exe` dan lampirkan ke GitHub release. `build.ps1` adalah skrip maintainer, bukan yang dijalankan pengguna.
- `site/public/index.html` `#start`: Linux satu perintah; catatan WSL 2 untuk Windows; .deb/.rpm/AppImage sebagai alternatif. README baris 33–75: jalur Linux + Windows setup.exe + catatan WSL 2 untuk PS1/PS2/N64-KMC.
- CI: `check.yml` (ruff + pytest di ubuntu + windows), `site.yml` deploy `site/public` ke Pages tiap push main yang menyentuhnya.

## Decisions (agreed with user)
1. Cakupan: Linux + Windows, perubahan install.sh minimal.
2. `install.sh` = khusus Linux (ingat: dijalankan di terminal WSL-Ubuntu di mesin Windows = tetap Linux). Native Windows = `setup.exe`, bukan install.sh.
3. Jalur utama Linux tetap `curl|bash`; .deb/.rpm/AppImage tetap alternatif. Tidak ada perubahan strategi distribusi.

## Task list (ordered, minimal diffs)

### 1. Harden `site/public/install.sh` (satu-satunya file logika yang berubah)
Prinsip: hanya pesan, cek prasyarat, dan keluar-graceful; urutan langkah tetap. Usulan edit konkret:
1. Pesan penolakan OS/arch yang bisa ditindaklanjuti:
   - Non-Linux: sebutkan Windows → unduh `decompdecomp-<ver>-windows-x64-setup.exe` dari releases (bukan `build.ps1`); macOS → belum didukung. Pertahankan `fail` (exit 2).
   - Non-x86_64: pertahankan alasan compiler x86 + `toolchains.unsupported_cpu()` selaras.
2. `curl` hilang: ganti `fail "curl is required"` dengan perintah install per-distro (`apt`/`dnf`/`pacman`/`zypper`) mengikuti pola blok GTK yang sudah ada (pakai `/etc/os-release`), lalu exit.
3. Tambah flag `--yes` (opsional, default tetap interaktif): lewati semua `ask()` untuk pengguna awam yang hanya ingin next-next. Tanpa flag ini perilaku tidak berubah (termasuk `has_tty || return 0` untuk pipa curl).
4. Setelah `uv tool install`, verifikasi `decomp` dapat dijalankan (`BIN/decomp --help` cepat atau `command -v decomp`); bila tidak di PATH, cetak instruksi konkret (buka terminal baru / `export PATH="$HOME/.local/bin:$PATH"`), jangan diam.
5. Rapikan teks `ask()` uv dan GTK/WebKit: satu baris "apa + kenapa + perintah persis yang akan jalan". Jangan ubah logika deteksi.
6. `decomp setup` gagal sebagian: ia sudah lanjut per-toolchain dan lapor (`cli.setup` try/except). Di install.sh, setelahnya ringkas status ("compilers ok / ada yang kurang — buka Compilers & tools di app") alih-alih menakut-nakuti dengan traceback; exit code tetap 0 bila app terinstal (setup dapat diulang dari app).
7. Final message: selalu cetak 3 baris tetap — cara start (`decomp` / apps menu), lokasi proyek (`~/Decomp`), cara uninstall (`uv tool uninstall decompnet`). Hari ini uninstall hanya di komentar header baris 14.
8. Jaga kompatibilitas `updates.py`: baris `VERSION="x"` tetap format `^VERSION="..."` di awal baris; channel/branch/commit logic, `SPEC`, dan opsi `--source/--no-start` tidak berubah.

### 2. Windows: tanpa ubah `build.ps1`, selaraskan panduan saja
1. README + `site/public/index.html#start`: langkah Windows = (a) unduh `decompdecomp-<ver>-windows-x64-setup.exe` dari releases, (b) buka, setujui WebView2/.NET bila diminta, (c) catatan WSL 2 satu perintah (`wsl --install`) hanya untuk game PS1/PS2/N64-KMC. Verifikasi nama artefak persis dengan `release.yml` job windows.
2. Pesan penolakan non-Linux di install.sh (task 1.1) menautkan ke halaman releases yang sama, agar pengguna Git Bash yang salah jalan tidak buntu.

### 3. Kunci versi (mencegah drift yang merusak self-update)
- Tambah test kecil (mis. di `tests/test_updates.py` atau file baru): `VERSION=` di `site/public/install.sh` == `version` di `pyproject.toml` == `decompnet.__version__`. Alasan: `updates._check_release` mem-parse VERSION dari install.sh di main sebagai sumber kebenaran versi terbaru.
- Alternatif tanpa test baru: CI `check.yml` step `bash -n` + perbandingan versi via grep. Pilih satu (rekomendasi: test pytest, karena repo mensyaratkan "every behaviour change gets a test").

### 4. CI/lint untuk skrip (murah, tanpa mengubah perilaku)
- `bash -n site/public/install.sh` sebagai step `check.yml` (syntax). `shellcheck` hanya bila runner menyediakannya tanpa instal baru; jika tidak, skip dengan alasan di komentar workflow (jangan tambah dependensi).
- `uv run ruff check src tests projects` tetap harus lolos; test baru mengikuti konvensi nama kalimat-perilaku (`CONVENTIONS.md` §9).

## Failure modes covered
| Kondisi | Perilaku hari ini | Perilaku yang diinginkan |
|---|---|---|
| Non-Linux (Git Bash/macOS) | `fail "runs on Linux..."` generik | Sebut artefak Windows yang benar / status macOS + tautan releases |
| CPU non-x86_64 | fail jelas | Tetap, selaras dengan `unsupported_cpu()` |
| `curl` absen | `fail "curl is required"` | Perintah install curl per-distro |
| Tanpa TTY (otomasi) | `ask` auto-yes | Tetap + flag `--yes` terdokumentasi |
| `uv` gagal / PATH belum termuat | `fail` hanya untuk kasus PATH | Verifikasi pasca-install + instruksi buka terminal baru |
| GTK/WebKitGTK absen | tanya install per-distro | Sama, teks lebih jelas; non-fatal (app lapor via `window.missing()`) |
| Compiler/tool gagal unduh | `decomp setup` sudah per-toolchain lanjut | Ringkasan akhir + "ulangi dari Compilers & tools", exit tetap sukses bila app ok |
| Paket .deb/.rpm lama dobel | tanya hapus | Sama (jangan hapus otomatis) |
| Tanpa display (SSH) | pesan jalankan di mesin itu | Tetap |

## Rollout / migration
- Branch `feat/easy-install` (konvensi `<type>/<short-name>`, CI labels). Satu PR: `install.sh` + test versi + README/situs + CI step.
- Tidak ada migrasi data: `install.json` channel, `~/Decomp`, dan `DECOMP_PACKAGE` tidak berubah. Re-run installer idempoten (`--force`, `stop` versi lama, shortcut overwrite).
- Rilis: `packaging/release.sh` tetap bump 4 tempat; `site.yml` otomatis publish perubahan situs.

## Validation plan
1. `bash -n site/public/install.sh`; `uv run ruff check src tests projects`.
2. Test baru versi sinkron lolos: `UV_PROJECT_ENVIRONMENT=<scratch venv> uv run pytest -q tests/test_updates.py` (repo: jangan rebuild `.venv` checkout yang dipakai app berjalan).
3. Uji manual di VM/container fresh (matriks minimal): Ubuntu/Debian (apt path), Fedora (dnf path): `curl|bash` → app terbuka; tutup, re-run installer → idempoten; `decomp` ada di PATH setelah shell baru; entri apps menu ada; `decomp setup` compiler terinstal; tanpa DISPLAY → pesan SSH.
4. Uji pesan penolakan: `OS` non-Linux dan `uname -m` non-x86_64 → exit non-0 dengan instruksi yang benar (simulasikan via fungsi, tanpa ubah mesin).
5. Windows: verifikasi akhir-ke-awal — nama file setup.exe di README/situs sama dengan artefak `release.yml`; langkah WSL 2 diikuti di mesin Windows sesuai README.
6. `DECOMP_HOME=<scratch> DECOMP_TRAY=0` untuk eksperimen apa pun; jangan sentuh `~/Decomp` pengguna atau app yang berjalan.

## Open questions (none blocking)
- `--yes`: apakah juga men-skip tawaran hapus .deb/.rpm lama (rekomendasi: ya, skip = tidak hapus, paling aman untuk awam) — putuskan saat implementasi, satu baris komentar `why`.
- Apakah ringkasan akhir setup perlu mencantumkan `decomp update` untuk update mandiri — satu baris bila ya.
