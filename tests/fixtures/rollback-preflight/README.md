# Previous-release catalog data for the rollback preflight tests

Each directory holds the packaged catalog data of one published release,
copied byte for byte from the release tag with `git show <commit>:<path>`
(one file per command), under its resource-relative path
(`src/charmer/data/<path>`):

| Directory | Tag      | Source commit                              |
| --------- | -------- | ------------------------------------------ |
| `2.2.1/`  | `v2.2.1` | `acf9bd5238058ebcdfcef8f62ac101c581ebca47` |
| `2.3.0/`  | `v2.3.0` | `2fe786954ee93e18a2d25ab722bc602c122bc3e0` |

Files: `version.json`, `catalog/gateway.json`, `catalog/models.json`,
`catalog/native-contract.json`, `catalog/providers.json`,
`catalog/retired.json`, `catalog/roles.json` and the eight seed profiles
under `catalog/profiles/`. The schemas, `settings.json` and the role
prompts come from the checkout under test, as the preflight takes them from
the running release.

These files are frozen: never edit or regenerate them.
