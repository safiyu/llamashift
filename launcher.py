"""Console entry point for pip-installed LlamaShift (the `llamashift` command).

The server modules read ./config.json and write logs relative to the working directory, so an
installed copy runs from a data directory instead: $LLAMASHIFT_HOME, default ~/.llamashift.
Checkout users keep using `python server.py` (or main.py) from the repo as before.
"""
import os
import shutil
import sys

PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("LLAMASHIFT_HOME", "~/.llamashift")))


def prepare_data_dir(home: str | None = None) -> str:
    """Creates the data directory and seeds config.json from the shipped template; never overwrites."""
    home = home or data_dir()
    os.makedirs(home, exist_ok=True)
    config_path = os.path.join(home, "config.json")
    template = os.path.join(PKG_DIR, "config.json.template")
    if not os.path.exists(config_path) and os.path.isfile(template):
        shutil.copyfile(template, config_path)
    return home


def run() -> None:
    if "--version" in sys.argv[1:]:
        sys.path.insert(0, PKG_DIR)
        from version import __version__, API_VERSION
        print(f"llamashift {__version__} (api {API_VERSION})")
        return
    os.chdir(prepare_data_dir())
    sys.path.insert(0, PKG_DIR)  # the server modules import each other by flat name
    from main import main
    main()


if __name__ == "__main__":
    run()
