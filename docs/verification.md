# Verification

The test suite builds real temporary Git histories and checks that merge rows contain parent edges, valid lane targets, and repeated split/merge ancestry.

Browser verification should confirm that the page has SVG paths, commit dots, merge dots, branch-head labels, and no visible control clutter.

Installer verification:

```sh
sh -n install.sh scripts/install-gbp scripts/gbp
GBP_REPO_URL="file://$PWD" GBP_NO_RUN=1 HOME="$(mktemp -d)" sh install.sh
```

The second command proves the paste-on-any-machine installer can clone the repo, install `gbp`, and exit without launching the server.
