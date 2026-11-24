# PSI

[中文说明](README.zh-CN.md)

PSI is a local web workbench for reviewing questions, decisions and processing records. Open a question to see its evidence, the proposed next step and your draft reply. The included example uses fictional records and runs in your browser with Python's standard library.

## Run the workbench

On macOS or Linux with Python 3.12 or later, run:

```sh
git clone https://github.com/Samuel-Shek/AGI.git
cd AGI
python3 -B web/psi_sync_center.py
```

Open **[http://127.0.0.1:8767/](http://127.0.0.1:8767/)** on the same computer. Select a question, inspect its evidence and try writing a draft. The example uses temporary local storage for this session. Press Ctrl+C in the terminal to stop it.

To keep drafts between sessions or choose another port, see [running the workbench](docs/INSTALL.md).

## Source and privacy

The browser connects only to the loopback server. The default run uses the bundled example without reading personal activity, connecting to a local agent or registering a background service. Contributors can also inspect the Python Core and its source-admission, privacy and audit controls. Connecting an existing Core requires explicit setup. [Public source scope](docs/PUBLIC_SOURCE_SCOPE.md) · [Privacy](docs/PRIVACY.md)

The automated checks exercise the web interface and selected Core contracts with synthetic inputs. Real-world benefits of the experimental reflection and repair workflows still require separate evidence. [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

## License

[AGPL-3.0-only](LICENSE). Third-party notices and the separately licensed Hermes component are listed in [NOTICE](NOTICE) and the [vendor provenance](core/evolution/ai_ceo/vendor/PROVENANCE.json).
