# Checking a public web release

Use a clean checkout of the proposed public revision. Run the [workflow checks](../.github/workflows/ci.yml) with synthetic data and confirm the release privacy scan passes before starting the demo.

1. Follow the [quick start](INSTALL.md#workbench-demo) with a fresh user data directory. Verify the browser opens on loopback, displays fictional items and saves a local draft.
2. Stop the server and start another default session. Verify the example starts fresh. With an explicit private draft directory, verify the draft survives a restart.
3. Check the public source tree, workflow, README and repository description agree on the browser entry point. The current release contains no PSI desktop bundle, native UI package or desktop installer.
4. Check that the published revision matches the tested commit, the hosted checks passed and the same quick start works from an anonymous checkout.

Use only synthetic inputs for public reports. Keep private policies, evidence, credentials, live databases and operator deployment artifacts outside the release.
