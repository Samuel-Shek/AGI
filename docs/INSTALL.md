# Run PSI in a browser

The local workbench uses Python 3.12 or later on macOS or Linux. The Python standard library is sufficient. Run the commands below from the root of the source checkout.

## Workbench demo

```sh
python3 -B web/psi_sync_center.py
```

Open **[http://127.0.0.1:8767/](http://127.0.0.1:8767/)** on this computer. The default dataset contains fictional questions, evidence and processing records. Choose a question, type a draft and switch to another question and back to see your draft again. The session uses a fresh temporary directory. Ctrl+C stops the server and releases that directory on normal exit.

To keep example drafts across restarts, provide a dedicated directory that you own. The workbench creates it with private permissions if necessary.

```sh
python3 -B web/psi_sync_center.py --demo --data-dir ~/.psi-demo
```

If the default port is occupied, keep the existing service running and choose a different port. Open the address printed by the command.

```sh
python3 -B web/psi_sync_center.py --port 8877
```

Use `--fixture PATH` for your own synthetic dataset in the [example format](../web/tests/fixtures/demo.json). Fixture mode uses isolated local records and never hands a reply to an external agent.

## Existing local Core

On a Mac already configured with a compatible, separately authorized PSI Core, `--live` explicitly selects that integration. The workbench reads through the existing Core controller and its grants. The demo and its temporary storage are independent of that setup. See the [source scope](PUBLIC_SOURCE_SCOPE.md) for integration limits.

## Development checks

The [public workflow](../.github/workflows/ci.yml) lists the web, privacy and portable Core checks. Use synthetic inputs and a fresh working directory for tests.
