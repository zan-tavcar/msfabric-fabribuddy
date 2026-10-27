# Update

**Check_FabriBuddy_Version** (in the *Maintenance* folder) tells you whether a newer release is
out. Run it any time.

## Updating the scanner

Import the latest
[`Deploy_FabriBuddy.ipynb`](https://github.com/zan-tavcar/msfabric-fabribuddy/raw/main/scripts/Deploy_FabriBuddy.ipynb)
over the old one, and **Run all** with the service principal cell left empty.

It downloads the latest release, stores it in `FabriBuddy_DB` and updates the notebooks and the
pipeline. These are **kept**:

- The pipeline's [settings](settings.md)
- The schedule
- Everything already collected

The next scan uses the new version. To get it now, run **Load_FabriBuddy_E2E**.

## Updating the app

When a release changes the app, update it from your copy of the repository:

```bash
cd msfabric-fabribuddy
git pull
cd app
npm install
npx rayfin up --workspace-id <workspace-id>
```

The [release notes](https://github.com/zan-tavcar/msfabric-fabribuddy/releases) say when this is
needed.

## Which version am I on?

The first lines of a scan's output say `FabriBuddy <version>`, and so do the deploy notebook and
**Check_FabriBuddy_Version**.
