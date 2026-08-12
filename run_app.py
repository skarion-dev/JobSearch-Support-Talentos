"""
Start the Streamlit app from anywhere.

`streamlit run app/main.py` only works when the shell is already sitting in the
repo root, because that is what puts the repo on sys.path — run it from one
directory up and every `from app import ...` fails with ModuleNotFoundError.
That bites the scheduled task, the deploy script and anyone starting it from a
launcher config.

This chdir's to its own directory first, so the working directory is correct no
matter where it was invoked from.

    python run_app.py                 # port 3100
    python run_app.py --port 8080
"""
import os
import sys

import streamlit.web.cli as stcli

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = "3100"


def main() -> int:
    os.chdir(HERE)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)

    port = DEFAULT_PORT
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]

    sys.argv = [
        "streamlit", "run", os.path.join(HERE, "app", "main.py"),
        "--server.port", port,
        "--server.headless", "true",
    ]
    return stcli.main()


if __name__ == "__main__":
    sys.exit(main())
