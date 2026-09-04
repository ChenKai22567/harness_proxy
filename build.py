import os
import shutil
import tempfile
import PyInstaller.__main__


APP_NAME = "HarnessProxyLauncher"


def _copy_runtime_data(existing_dir: str, new_dir: str, default_config: str):
    """Carry user-owned runtime data into a freshly built release."""
    existing_config = os.path.join(existing_dir, "config.json")
    new_config = os.path.join(new_dir, "config.json")
    config_source = existing_config if os.path.isfile(existing_config) else default_config
    if os.path.isfile(config_source):
        shutil.copy2(config_source, new_config)

    existing_logs = os.path.join(existing_dir, "logs")
    if os.path.isdir(existing_logs):
        shutil.copytree(existing_logs, os.path.join(new_dir, "logs"), dirs_exist_ok=True)

    existing_sessions = os.path.join(existing_dir, "managed_sessions.json")
    if os.path.isfile(existing_sessions):
        shutil.copy2(existing_sessions, os.path.join(new_dir, "managed_sessions.json"))


def _kill_running_instances():
    if os.name == "nt":
        import subprocess
        try:
            # Never use /T here. Apps launched by the helper remain descendants
            # in Windows' process graph even with DETACHED_PROCESS; /T would
            # terminate Codex/Antigravity while merely replacing the launcher.
            subprocess.run(["taskkill", "/F", "/IM", f"{APP_NAME}.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        import time
        time.sleep(0.5)


def _publish_release(new_release: str, final_release: str, staging_root: str, stop_running_instances: bool = True):
    """Replace the release only after a complete build, with rollback on error."""
    if stop_running_instances:
        _kill_running_instances()
    import time
    for attempt in range(5):
        try:
            if os.path.isdir(final_release):
                shutil.rmtree(final_release)
            break
        except Exception:
            if stop_running_instances:
                _kill_running_instances()
            time.sleep(0.5)

    for attempt in range(5):
        try:
            if not os.path.exists(final_release):
                os.replace(new_release, final_release)
                return
        except Exception:
            if stop_running_instances:
                _kill_running_instances()
            time.sleep(0.5)

    shutil.copytree(new_release, final_release, dirs_exist_ok=True)

def build_exe():
    app_root = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(app_root, "dist")
    build_dir = os.path.join(app_root, "build")
    icon_path = os.path.join(app_root, "assets", "icon.ico")
    main_script = os.path.join(app_root, "main.py")
    final_release = os.path.join(dist_dir, APP_NAME)
    local_config = os.path.join(app_root, "config.json")
    example_config = os.path.join(app_root, "config.example.json")
    default_config = local_config if os.path.isfile(local_config) else example_config

    print("==================================================")
    print("      Building Harness代理启动 (PyInstaller)       ")
    print("==================================================")

    # A previously launched frozen Tk app can leak its private Tcl/Tk paths
    # into the parent environment.  PyInstaller would then inspect the old
    # release instead of the current Python installation and omit ``_tk_data``.
    os.environ.pop("TCL_LIBRARY", None)
    os.environ.pop("TK_LIBRARY", None)

    os.makedirs(dist_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".harness-release-", dir=app_root) as staging_root:
        staging_dist = os.path.join(staging_root, "dist")

        # Collect CustomTkinter's theme/font assets, but avoid bundling every
        # Pillow plugin and every non-Windows pystray backend.  Python imports
        # already discover PIL.Image/ImageDraw; the one hidden backend below is
        # the only dynamic import needed on this platform.
        args = [
            main_script,
            "--noconsole",
            "--onedir",
            f"--name={APP_NAME}",
            f"--icon={icon_path}",
            f"--add-data={os.path.join(app_root, 'assets')};assets",
            "--collect-all=customtkinter",
            "--hidden-import=pystray._win32",
            "--exclude-module=numpy",
            "--exclude-module=tkinter.test",
            f"--distpath={staging_dist}",
            f"--workpath={build_dir}",
            "--clean",
            "-y",
        ]

        print("Running PyInstaller with arguments:")
        print(" ".join(args))
        PyInstaller.__main__.run(args)

        new_release = os.path.join(staging_dist, APP_NAME)
        if not os.path.isfile(os.path.join(new_release, f"{APP_NAME}.exe")):
            raise RuntimeError("PyInstaller completed without producing the expected executable")

        _copy_runtime_data(final_release, new_release, default_config)
        _publish_release(new_release, final_release, staging_root)

    print("\n[SUCCESS] Build complete! Executable located at:")
    print(os.path.join(final_release, f"{APP_NAME}.exe"))

if __name__ == "__main__":
    build_exe()
