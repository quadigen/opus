# Kinemium developer platform module

The Kinemium module adds a developer platform API to Synapse under
`/_kinemium/v1`. It provides developer profiles, game identities, API tokens,
datastores, a package registry, and a marketplace.

## Configuration

Add the module to `homeserver.yaml`:

```yaml
modules:
  - module: kinemium_module.KinemiumModule
    config:
      storage_path: /var/lib/synapse/kinemium
```

`storage_path` is required and must be an absolute, writable directory. It
stores uploaded package and marketplace archives. Only the `local` storage
backend is supported.

Optional settings and their defaults:

| Key | Default |
| --- | --- |
| `max_archive_bytes` | `8 MiB` |
| `max_json_body_bytes` | `16 MiB` |
| `max_archive_uncompressed_bytes` | `64 MiB` |
| `max_archive_entries` | `10000` |
| `max_media_bytes` | `4 MiB` |
| `max_emoji_bytes` | `1 MiB` |
| `max_space_emojis` | `50` |
| `max_datastore_key_length` | `128` |
| `max_datastore_value_bytes` | `64 KiB` |
| `max_datastore_batch` | `50` |
| `max_datastores_per_game` | `32` |
| `max_datastores_per_user` | `128` |
| `max_entries_per_store` | `100000` |
| `max_ordered_entries_per_store` | `100000` |
| `max_api_tokens_per_user` | `25` |
| `max_marketplace_versions_per_item` | `100` |
| `max_package_versions` | `1000` |
| `max_listing_tags` | `20` |
| `max_review_length` | `4000` |
| `max_dependency_count` | `100` |
| `max_resolve_packages` | `100` |
| `pagination_default_limit` | `20` |
| `pagination_max_limit` | `100` |
| `rate_limit_per_second` | `20` |
| `rate_limit_burst` | `60` |
| `datastore_rate_limit_per_second` | `50` |
| `datastore_rate_limit_burst` | `200` |
| `enable_cors` | `true` |

Byte sizes may be integers or strings such as `"64 KiB"`, `"8M"`, or `"1G"`.

The module supports PostgreSQL and SQLite. PostgreSQL is recommended for
production. Apply the normal Synapse schema migration before starting the
server; the module tables are in schema deltas 95 and 96.

## Authentication

Requests may use a normal Matrix access token:

```http
Authorization: Bearer <matrix_access_token>
```

or a Kinemium API token:

```http
Authorization: Bearer knm_<token>
```

API tokens are created through `POST /me/tokens` and are shown only once. A
token can be bound to a game with `gameId`; game-bound tokens can only access
that game's datastores and cannot manage games.

Matrix tokens have all scopes. API tokens receive the scopes requested at
creation time:

`account:read`, `account:write`, `marketplace:read`,
`marketplace:write`, `marketplace:acquire`, `datastores:read`,
`datastores:write`, `packages:read`, `packages:write`, `spaces:read`,
`spaces:write`, `*`

Matrix user IDs in paths must be URL-encoded when they contain `@` or `:`.

## Errors

Errors are JSON objects with an HTTP status and an `errcode`:

```json
{"errcode": "KINEMIUM_CONFLICT", "error": "Entry version conflict.", "currentVersion": 2}
```

Common error codes are `KINEMIUM_BAD_JSON`, `KINEMIUM_UNAUTHORIZED`,
`KINEMIUM_FORBIDDEN`, `KINEMIUM_NOT_FOUND`, `KINEMIUM_CONFLICT`,
`KINEMIUM_RATE_LIMITED`, `KINEMIUM_PAYLOAD_TOO_LARGE`, and
`KINEMIUM_INTERNAL_ERROR`.

## Accounts

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/me` | Get the current developer profile. |
| `PATCH` | `/me` | Update profile fields. |
| `GET` | `/me/games` | List owned games. |
| `POST` | `/me/games` | Create a game. Body: `{"name": "My game"}`. |
| `GET` | `/me/tokens` | List API tokens. Add `?includeRevoked=true` to include revoked tokens. |
| `POST` | `/me/tokens` | Create an API token. Body: `{"name": "CI", "scopes": ["datastores:read"], "gameId": "..."}`. |
| `DELETE` | `/me/tokens/{tokenId}` | Revoke an API token. |
| `GET` | `/accounts/{userId}` | Get a public profile. |
| `GET` | `/accounts/{userId}/marketplace` | List that developer's published marketplace items. |
| `GET` | `/accounts/{userId}/games` | List that developer's games. |
| `POST` | `/me/banner` | Upload an account banner image. Returns the updated profile. |
| `DELETE` | `/me/banner` | Remove the account banner. |
| `POST` | `/me/avatar` | Upload an avatar and set it as the Matrix profile avatar. |

`PATCH /me` accepts `displayName`, `bio`, `website`, `publicProfile`,
`publicMarketplace`, `bannerUrl`, `accentColor`, `statusText`, `statusEmoji`,
and `pronouns`. `displayName` updates the Matrix display name through Synapse.
`accentColor` is a `#RRGGBB` string. `bannerUrl` is an `mxc://` URL.

Profile image uploads accept PNG, JPEG, GIF, and WebP up to `max_media_bytes`.
The image type is detected from the file contents; the request body is the raw
image, not multipart.

## Spaces

Spaces are Matrix rooms whose `m.room.create` content has `"type": "m.space"`.
The API only exposes spaces the caller has joined. Customization is stored in
room state and follows normal Matrix power levels.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/spaces` | List joined spaces with customization. |
| `GET` | `/spaces/{roomId}/profile` | Get a space profile. |
| `PATCH` | `/spaces/{roomId}/profile` | Update `name`, `topic`, `avatarUrl`, `bannerUrl`, `accentColor`, or `description`. |
| `POST` | `/spaces/{roomId}/avatar` | Upload and set the space avatar. |
| `POST` | `/spaces/{roomId}/banner` | Upload and set the space banner. |
| `GET` | `/spaces/{roomId}/emojis` | List custom emojis. |
| `POST` | `/spaces/{roomId}/emojis` | Upload a custom emoji. Uses `multipart/mixed` with a `shortcode` field and one image file part. |
| `DELETE` | `/spaces/{roomId}/emojis/{shortcode}` | Delete a custom emoji. |

`name`, `topic`, and `avatarUrl` map to the native `m.room.name`,
`m.room.topic`, and `m.room.avatar` state events. `bannerUrl`, `accentColor`,
and `description` are stored in `com.quadigen.kinemium.space.profile`; custom
emojis are stored in `com.quadigen.kinemium.space.emojis`. Emoji images may be
GIFs, in which case the API reports `animated: true`. The number of custom
emojis per space is limited by `max_space_emojis`, and each image must fit in
`max_emoji_bytes`.

## Datastores

Datastores belong to a game. A game-bound API token can omit `gameId` when
creating a datastore; a Matrix token must provide it.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/datastores` | List datastores. Optional `gameId`, `cursor`, `limit`. |
| `POST` | `/datastores` | Create a datastore. Body: `gameId`, `name`, optional `description`, `config`. |
| `GET` | `/datastores/{storeId}` | Get datastore metadata. |
| `PATCH` | `/datastores/{storeId}` | Update `name`, `description`, or `config`. |
| `DELETE` | `/datastores/{storeId}` | Delete a datastore and its entries. |
| `GET` | `/datastores/{storeId}/entries` | List entry keys. Optional `prefix`, `cursor`, `limit`. |
| `GET` | `/datastores/{storeId}/entries/{key}` | Read one entry. Returns an `ETag`. |
| `PUT` | `/datastores/{storeId}/entries/{key}` | Write an entry. Body: `{"value": ..., "expectedVersion": 0}`. |
| `PATCH` | `/datastores/{storeId}/entries/{key}` | Update an entry. Body: `{"operation": "increment", "amount": 1}`. |
| `DELETE` | `/datastores/{storeId}/entries/{key}` | Delete an entry. |
| `POST` | `/datastores/{storeId}/entries/batch-read` | Read up to `max_datastore_batch` keys. |
| `POST` | `/datastores/{storeId}/entries/batch-write` | Atomically write up to `max_datastore_batch` entries. |
| `GET` | `/datastores/{storeId}/ordered-entries` | List ordered entries by score. |
| `PUT` | `/datastores/{storeId}/ordered-entries/{key}` | Write an ordered entry. Body: `score`, optional `value`, optional `expectedVersion`. |

Entry values are arbitrary JSON within the configured value size. Versions
start at 1 and increase on each write. Clients may send `If-Match: "<version>"`
or `expectedVersion`; a mismatch returns `KINEMIUM_CONFLICT`.

Patch operations are `set`, `increment`, `merge`, and `append`. `increment`
requires a numeric current value, `merge` requires an object, and `append`
requires an array.

Ordered entries support `order=asc|desc`, `minScore`, `maxScore`, `cursor`, and
`limit` when listing.

## Packages

Package namespaces are derived from the developer's Matrix localpart,
lowercased. A manifest namespace, if present, must match it.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/packages` | Search packages. Optional `q`, `namespace`, `sort`, `includeYanked`, `cursor`, `limit`. |
| `POST` | `/packages` | Publish a package version. |
| `POST` | `/packages/resolve` | Resolve dependencies. |
| `GET` | `/packages/{namespace}/{name}` | Get a package and its versions. |
| `PATCH` | `/packages/{namespace}/{name}` | Update package metadata or deprecate it. |
| `GET` | `/packages/{namespace}/{name}/versions/{version}` | Get version metadata. |
| `POST` | `/packages/{namespace}/{name}/versions/{version}/yank` | Yank a version. |
| `GET` | `/packages/{namespace}/{name}/versions/{version}/download` | Download a version archive. |

Publishing uses `multipart/mixed` with a `manifest` field and one archive file
part. Synapse rejects `multipart/form-data` before module routing, so clients
must use `multipart/mixed`.

A manifest is a JSON object:

```json
{
  "name": "tool",
  "version": "1.2.0",
  "description": "A test package",
  "entrypoint": "src/main.luau",
  "engineConstraint": "^1.0.0",
  "dependencies": {"alice/core": "^2.0.0"}
}
```

Archives must be ZIP or tar, must not contain absolute paths, `..`, links,
encrypted entries, or duplicate names, and must stay within the archive limits.

Dependency resolution accepts:

```json
{
  "requirements": [
    {"namespace": "alice", "name": "tool", "constraint": "^1.0.0"}
  ]
}
```

Resolution accumulates constraints, selects the highest satisfying version of
each package, and follows that version's dependencies. It does not solve
optional features or backtrack when a new constraint makes a previously
selected version invalid.

## Marketplace

Marketplace items are free listings. Paid prices are rejected.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/marketplace` | Browse published public items. Optional `q`, `type`, `tag`, `sort`, `cursor`, `limit`. |
| `POST` | `/marketplace/items` | Create a draft item. |
| `GET` | `/marketplace/items/{itemId}` | Get an item. Unlisted items are accessible by direct ID; private items are owner-only. |
| `PATCH` | `/marketplace/items/{itemId}` | Update item metadata. |
| `DELETE` | `/marketplace/items/{itemId}` | Delete an item. |
| `POST` | `/marketplace/items/{itemId}/publish` | Publish an item. |
| `POST` | `/marketplace/items/{itemId}/unpublish` | Unpublish an item. |
| `POST` | `/marketplace/items/{itemId}/versions` | Upload an item version. Uses `multipart/mixed` with metadata fields `version` and `changelog`, plus one file part. |
| `GET` | `/marketplace/items/{itemId}/versions` | List versions. |
| `GET` | `/marketplace/items/{itemId}/versions/{version}/download` | Download a version. |
| `POST` | `/marketplace/items/{itemId}/acquire` | Acquire a published item. Free items only. |
| `GET` | `/marketplace/acquisitions` | List the current user's acquisitions. |
| `GET` | `/marketplace/items/{itemId}/reviews` | List reviews. |
| `POST` | `/marketplace/items/{itemId}/reviews` | Create or replace a review. Requires acquisition. Body: `rating` 1–5 and optional `body`. |

Creating an item accepts `name`, `description`, `itemType`, `tags`,
`visibility`, optional `packageId`, and optional `packageVersion`. Item types
are `model`, `plugin`, `template`, `tool`, and `package`. Visibility is
`public`, `unlisted`, or `private`.

Reviews are one per user per item. Updating a review replaces the previous
rating and body.

## Storage and limits

Uploaded archives are written atomically under `storage_path` with a
server-generated key. Downloads are served with the original sanitized filename
and a SHA-256 digest is stored in the database. Database failures remove newly
written archives; if a later step fails after a package row is created, the
package row may remain without the failed version.

Rate limits are applied in memory per worker. A multi-worker deployment applies
limits independently in each worker.

## Tests

The integration tests use the Synapse test harness:

```sh
.venv/bin/python -m twisted.trial tests.kinemium.test_kinemium
```

For PostgreSQL:

```sh
SYNAPSE_POSTGRES=1 \
SYNAPSE_POSTGRES_HOST=127.0.0.1 \
SYNAPSE_POSTGRES_USER=postgres \
SYNAPSE_POSTGRES_PASSWORD=postgres \
.venv/bin/python -m twisted.trial tests.kinemium.test_kinemium
```
