# Kinemium developer platform module

The Kinemium module adds a developer platform API to Synapse under
`/_kinemium/v1`. It provides developer profiles, game identities, API tokens,
a package registry, a package-backed marketplace, free game-server hosting, and
Studio collaboration signaling.

## Hosting

Opus is not tied to `https://opus.quadigen.com`. The module runs on any Synapse
instance. See [Hosting Opus locally](kinemium-local-hosting.md) for a complete
local or self-hosted setup, including the Opus client, CORS, HTTPS, and reverse
proxy notes.

The examples below use the hosted deployment URL. Replace
`https://opus.quadigen.com` with your own base URL when self-hosting.

## Breaking changes

Datastores have been removed entirely. The `/datastores` endpoints,
`datastores:read` and `datastores:write` scopes, datastore configuration keys,
and datastore database tables are gone.

The marketplace now lists packages directly. Standalone marketplace items and
item versions were removed, including the `/marketplace/items` endpoints. Use
the package endpoints to publish and manage listings, then use
`/marketplace/{namespace}/{name}` to browse, acquire, and review them.

Schema delta 97 folds `item_type`, `tags`, `visibility`, and `published_at`
into `kinemium_packages`, then drops the datastore and legacy marketplace
tables. Existing package rows are treated as public `package` listings.

Schema delta 98 adds package review state. Existing packages are marked
`approved`; newly published packages start `pending` and are hidden from the
marketplace until a Matrix server administrator approves them. Schema delta 103
adds independent review state for versions.

Schema delta 99 adds `icon_url` and `banner_url` to packages.
Schema delta 100 adds package reactions and comments.
Schema delta 101 adds package download counts.
Schema delta 102 adds package lifecycle metadata, screenshots, platform events,
moderation reports, administrator audit logging, notifications, webhooks, and
idempotency records.
Schema delta 104 adds space-owned package namespaces and packages, plus the
administrator-managed `official` package marker.
Schema delta 105 adds game profile media and free game-server hosting records.
Schema delta 106 adds Studio collaboration sessions, participants, invitations,
join requests, and ephemeral WebRTC signaling messages.

## Configuration

Add the module to `homeserver.yaml`:

```yaml
modules:
  - module: kinemium_module.KinemiumModule
    config:
      storage_path: /var/lib/synapse/kinemium
```

`storage_path` is required and must be an absolute, writable directory. It
stores uploaded package archives. Only the `local` storage backend is
supported.

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
| `max_api_tokens_per_user` | `25` |
| `max_webhooks_per_user` | `20` |
| `max_collab_participants` | `16` |
| `collab_session_ttl_seconds` | `21600` |
| `collab_invite_ttl_seconds` | `86400` |
| `collab_signal_ttl_seconds` | `300` |
| `max_collab_signal_bytes` | `64 KiB` |
| `server_worker_token` | unset |
| `max_package_versions` | `1000` |
| `max_listing_tags` | `20` |
| `max_review_length` | `4000` |
| `max_dependency_count` | `100` |
| `max_resolve_packages` | `100` |
| `pagination_default_limit` | `20` |
| `pagination_max_limit` | `100` |
| `rate_limit_per_second` | `20` |
| `rate_limit_burst` | `60` |
| `enable_cors` | `true` |
| `cors_origins` | `["*"]` |

Byte sizes may be integers or strings such as `"64 KiB"`, `"8M"`, or `"1G"`.

The module supports PostgreSQL and SQLite. PostgreSQL is recommended for
production. Apply the normal Synapse schema migration before starting the
server; the module tables are in schema deltas 95 through 106.

## Authentication

Requests may use a normal Matrix access token:

```http
Authorization: Bearer <matrix_access_token>
```

or a Kinemium API token:

```http
Authorization: Bearer knm_<token>
```

API tokens are created through `POST /me/tokens` and are shown only once. They
must be sent in the `Authorization` header, not as an `access_token` query
parameter. A token can be bound to a game with `gameId`; game-bound tokens are scoped to
that game. They can read the bound game but cannot create, rename, or delete
games, list tokens bound to other games, or revoke tokens bound to other games.

Matrix tokens have all scopes. API tokens receive the scopes requested at
creation time:

`account:read`, `account:write`, `marketplace:read`,
`marketplace:write`, `marketplace:acquire`, `packages:read`,
`packages:write`, `spaces:read`, `spaces:write`, `*`

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
| `GET` | `/me/games/{gameId}` | Get one owned game. |
| `PATCH` | `/me/games/{gameId}` | Update an owned game's name, description, website, icon URL, or banner URL. |
| `DELETE` | `/me/games/{gameId}` | Delete an owned game. |
| `POST` | `/me/games/{gameId}/icon` | Upload a game icon. Raw PNG, JPEG, GIF, or WebP body. |
| `DELETE` | `/me/games/{gameId}/icon` | Remove the game icon. |
| `POST` | `/me/games/{gameId}/banner` | Upload a game banner. Raw PNG, JPEG, GIF, or WebP body. |
| `DELETE` | `/me/games/{gameId}/banner` | Remove the game banner. |
| `GET` | `/games/{gameId}` | Get a public game profile. |
| `POST` | `/me/games/{gameId}/server-requests` | Request a free hosted server. `multipart/mixed` with a `metadata` field and a `.kine` file. |
| `GET` | `/me/games/{gameId}/server-requests` | List this game's hosting requests. |
| `GET` | `/me/games/{gameId}/server-requests/{requestId}` | Get one hosting request. |
| `DELETE` | `/me/games/{gameId}/server-requests/{requestId}` | Cancel a pending or failed hosting request. |
| `GET` | `/me/games/{gameId}/servers` | List provisioned servers for this game. |
| `GET` | `/me/servers` | List all provisioned servers owned by the caller. |
| `GET` | `/me/tokens` | List API tokens. Add `?includeRevoked=true` to include revoked tokens. |
| `POST` | `/me/tokens` | Create an API token. Body: `{"name": "CI", "scopes": ["marketplace:read"], "gameId": "..."}`. |
| `DELETE` | `/me/tokens/{tokenId}` | Revoke an API token. |
| `GET` | `/accounts/{userId}` | Get a public profile. |
| `GET` | `/accounts/{userId}/marketplace` | List that developer's published packages. |
| `GET` | `/accounts/{userId}/games` | List that developer's games. |
| `POST` | `/me/banner` | Upload an account banner image. Returns the updated profile. |
| `DELETE` | `/me/banner` | Remove the account banner. |
| `POST` | `/me/avatar` | Upload an avatar and set it as the Matrix profile avatar. |
| `DELETE` | `/me/avatar` | Remove the Matrix profile avatar. |

`PATCH /me` accepts `displayName`, `bio`, `website`, `publicProfile`,
`publicMarketplace`, `bannerUrl`, `accentColor`, `statusText`, `statusEmoji`,
and `pronouns`. `displayName` updates the Matrix display name through Synapse.
`accentColor` is a `#RRGGBB` string. `bannerUrl` is an `mxc://` URL.

Profile image uploads accept PNG, JPEG, GIF, and WebP up to `max_media_bytes`.
The image type is detected from the file contents; the request body is the raw
image, not multipart.

Game responses include `gameId`, `ownerUserId`, `name`, `description`,
`iconUrl`, `bannerUrl`, `website`, `createdAt`, `updatedAt`, and `canManage`.
`PATCH /me/games/{gameId}` accepts any of `name`, `description`, `website`,
`iconUrl`, and `bannerUrl`; omitted fields are unchanged. Set `website`,
`iconUrl`, or `bannerUrl` to `null` to clear it. Game-bound tokens can update
their own game's description, website, icon, and banner, but cannot rename the
game. They can also only list hosting requests and servers for their own game.
Icons and banners use the same image formats and size limit as account images.
Game deletion also removes its hosting requests, server records, pending
hosting archives, and collaboration sessions.

## Game server hosting

Developers can request a free hosted server for a game. A request is queued for
the operator's provisioning worker; it does not start a worker by itself.
`POST /me/games/{gameId}/server-requests` uses `multipart/mixed` with one
`metadata` field and one `.kine` file part:

```text
--boundary
Content-Disposition: form-data; name="metadata"

{"region": "eu", "maxPlayers": 24, "note": "Weekend playtest"}
--boundary
Content-Disposition: form-data; name="file"; filename="game.kine"

<binary .kine contents>
--boundary--
```

`metadata` is optional. `region` defaults to `auto`, `maxPlayers` defaults to
`16`, and `note` defaults to an empty string. The `.kine` file must fit in
`max_archive_bytes`. A game can have at most one `pending` or `claimed` request
at a time. Requests move through `pending`, `claimed`, and `failed`; a failed
request can be cancelled by its owner and a new one submitted.

The provisioning worker polls and claims requests with the administrator API.
Administrators can authenticate normally, or set `server_worker_token` and send
`X-Kinemium-Worker-Token` plus an optional `X-Kinemium-Worker-Id` header. The
worker flow is:

1. `GET /admin/game-server-requests?status=pending` to find work.
2. `POST /admin/game-server-requests/{requestId}/claim` to reserve a request.
   The response includes a `downloadUrl`.
3. Download the `.kine` from that URL and start the worker process.
4. `POST /admin/game-server-requests/{requestId}/complete` with `address`,
   `port`, `region`, and optional `maxPlayers`. Completion creates the
   provisioned server record, deletes the queue entry, and removes the stored
   `.kine` file.
5. Use `PATCH /admin/game-servers/{serverId}` to report `starting`, `online`,
   `offline`, or `failed`, and `POST /admin/game-server-requests/{requestId}/fail`
   with a `reason` when provisioning cannot proceed.

The worker should only call `complete` after the new process is actually
running. If provisioning fails before completion, call `fail`; the `.kine`
archive is kept so the owner can retry or cancel the request.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/admin/game-server-requests` | List requests. Optional `status`, `gameId`, `owner`, `limit`, `cursor`. |
| `POST` | `/admin/game-server-requests/{requestId}/claim` | Claim a pending request. Body: optional `{"workerId": "..."}`. |
| `GET` | `/admin/game-server-requests/{requestId}/download` | Download the request's `.kine` file. |
| `POST` | `/admin/game-server-requests/{requestId}/complete` | Create the server, remove the queue entry, and delete the archive. |
| `POST` | `/admin/game-server-requests/{requestId}/fail` | Mark a request failed. Body: optional `{"reason": "..."}`. |
| `GET` | `/admin/game-servers` | List provisioned servers. Optional `status`, `gameId`, `limit`, `cursor`. |
| `PATCH` | `/admin/game-servers/{serverId}` | Update `status`, `address`, or `port` and refresh the heartbeat. |
| `DELETE` | `/admin/game-servers/{serverId}` | Remove a provisioned server record. |

Provisioned server responses include `serverId`, `gameId`, `ownerUserId`,
`requestId`, `workerId`, `region`, `address`, `port`, `maxPlayers`, `status`,
`createdAt`, `updatedAt`, and `lastHeartbeatAt`. Deleting a server record does
not stop the external process; the operator's worker is responsible for that.

## Studio collaboration

Studio collaboration lets a game owner start a temporary session, invite other
Opus users, approve join requests, and exchange WebRTC signaling messages. Opus
does not carry project data: after peers exchange SDP and ICE messages, the
Kinemium clients establish a direct encrypted WebRTC connection and synchronize
the project themselves.

A game can have one active collaboration session at a time. The host must own
the game. Starting a session returns a `joinCode`; treat it as a secret and
rotate it if it leaks. Rotating the join code invalidates the previous code.
Session join codes are stored as SHA-256 hashes. Sessions expire after
`collab_session_ttl_seconds`; a host heartbeat extends the session. Invitations
expire after `collab_invite_ttl_seconds` or when the session ends. Signals expire
after `collab_signal_ttl_seconds` and are deleted when the session ends. The
participant count includes the host.

The current Synapse fork has no WebSocket server support, so signaling uses
short HTTP polling. Clients should poll `GET /collab/sessions/{sessionId}/signals`
with the last `nextCursor` value and send messages with
`POST /collab/sessions/{sessionId}/signals`.

Direct invitations accept a local username or a full Matrix user ID. The target
must be a known user; unknown users are rejected.

Host endpoints:

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/me/games/{gameId}/collab-sessions` | Start a session. Body: optional `{"name": "...", "maxParticipants": 16}`. |
| `GET` | `/me/collab-sessions?role=hosted` | List sessions hosted by the caller. Optional `gameId`, `status`, `limit`, `cursor`. |
| `DELETE` | `/me/collab-sessions/{sessionId}` | Stop a session. Idempotent. |
| `POST` | `/me/collab-sessions/{sessionId}/join-code` | Rotate the join code. |
| `POST` | `/me/collab-sessions/{sessionId}/invites` | Invite by `{"username": "bob"}` or `{"userId": "@bob:server"}`. |
| `GET` | `/me/collab-sessions/{sessionId}/invites` | List session invitations. Optional `status`. |
| `DELETE` | `/me/collab-sessions/{sessionId}/invites/{inviteId}` | Revoke a pending invitation. |
| `GET` | `/me/collab-sessions/{sessionId}/join-requests` | List join requests. Optional `status`. |
| `POST` | `/me/collab-sessions/{sessionId}/join-requests/{requestId}/approve` | Approve a join request. |
| `POST` | `/me/collab-sessions/{sessionId}/join-requests/{requestId}/deny` | Deny a join request. |
| `DELETE` | `/me/collab-sessions/{sessionId}/participants/{userId}` | Remove a guest. |

Guest endpoints:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/me/collab-invites` | List invitations for the caller. Optional `gameId`, `status`. |
| `POST` | `/me/collab-invites/{inviteId}/accept` | Accept an invitation. |
| `POST` | `/me/collab-invites/{inviteId}/decline` | Decline an invitation. |
| `POST` | `/collab/join` | Request to join with `{"code": "cjoin_..."}`. |
| `GET` | `/me/collab-join-requests` | List the caller's join requests. Optional `gameId`, `status`. |
| `POST` | `/me/collab-join-requests/{requestId}/cancel` | Cancel a pending join request. |
| `GET` | `/me/collab-sessions?role=joined` | List active sessions the caller has joined. |

Participant endpoints:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/collab/sessions/{sessionId}` | Get session details. |
| `GET` | `/collab/sessions/{sessionId}/participants` | List participants. |
| `POST` | `/collab/sessions/{sessionId}/heartbeat` | Refresh the caller's heartbeat. Host heartbeats extend the session. |
| `POST` | `/collab/sessions/{sessionId}/leave` | Leave the session. The host must stop it instead. |
| `POST` | `/collab/sessions/{sessionId}/signals` | Send a signal. Body: `{"kind": "offer", "payload": {...}, "toUserId": "..."}`. Omit `toUserId` to broadcast. |
| `GET` | `/collab/sessions/{sessionId}/signals?after=0` | Poll signals. Returns `signals`, `sessionStatus`, `hasMore`, and `nextCursor`. |

Signals are JSON objects with a `kind` such as `offer`, `answer`, or
`ice-candidate`, plus a `payload` object. The payload is limited by
`max_collab_signal_bytes`. Only joined participants can send or receive signals;
broadcast signals are visible to every joined participant, while direct signals
require the target to be a joined participant. Signal bodies are not project
data and should not be used for file transfer.

Session status is `active`, `ended`, or `expired`. Invitation status is
`pending`, `accepted`, `declined`, `revoked`, or `expired`; join-request status is
`pending`, `approved`, `denied`, or `cancelled`.

Session responses include `sessionId`, `gameId`, `gameName`, `hostUserId`,
`name`, `status`, `role`, `participantCount`, `maxParticipants`,
`nextSignalSequence`, `createdAt`, `updatedAt`, `lastHeartbeatAt`, `expiresAt`,
`endedAt`, and `endedBy`. The `joinCode` is returned only when a session is
created or its code is rotated. Invitations and join requests include their
status and the related session/game metadata.

Collaboration operations emit normal Kinemium notifications with these types:
`collab.invite`, `collab.invite_accepted`, `collab.invite_declined`,
`collab.invite_revoked`, `collab.join_request`, `collab.join_approved`,
`collab.join_denied`, `collab.join_request_cancelled`, `collab.participant_left`,
`collab.kicked`, and `collab.session_ended`.

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
| `DELETE` | `/spaces/{roomId}/avatar` | Remove the space avatar. |
| `POST` | `/spaces/{roomId}/banner` | Upload and set the space banner. |
| `DELETE` | `/spaces/{roomId}/banner` | Remove the space banner. |
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

## Packages

Package namespaces default to the developer's Matrix localpart, lowercased.
Developers can claim additional namespaces with `POST /namespaces`; a manifest
`namespace`, when present, must be the caller's default namespace or one they
own. Pass `spaceId` when claiming a namespace to bind it to a Matrix space the
caller can manage. A space can own at most one namespace, and a namespace bound
to a space can only be used by that space's managers.

A manifest may include `spaceId`. Publishing with `spaceId` uses the space's
bound namespace when `namespace` is omitted, and requires permission to manage
the space. Publishing into a space-bound namespace without `spaceId` infers the
space from the namespace.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/packages` | Publish a package version. |
| `POST` | `/packages/resolve` | Resolve dependencies. |
| `GET` | `/packages/{namespace}/{name}` | Get a package, versions, social data, and creator. |
| `PATCH` | `/packages/{namespace}/{name}` | Update package metadata or deprecate it. |
| `DELETE` | `/packages/{namespace}/{name}` | Permanently delete an owned package. |
| `GET` | `/packages/{namespace}/{name}/versions` | List versions with pagination. Optional `includeYanked`, `limit`, `cursor`. |
| `GET` | `/packages/{namespace}/{name}/manifest` | Get the manifest for `?version=...` or the latest version. |
| `GET` | `/packages/{namespace}/{name}/readme` | Get the package readme. |
| `PUT` | `/packages/{namespace}/{name}/readme` | Set the readme. Body: `{"readme": "..."}`. |
| `GET` | `/packages/{namespace}/{name}/versions/{version}/changelog` | Get a version changelog. |
| `PUT` | `/packages/{namespace}/{name}/versions/{version}/changelog` | Set a version changelog. Body: `{"changelog": "..."}`. |
| `GET` | `/packages/{namespace}/{name}/dependents` | List packages that depend on this package. |
| `GET` | `/packages/{namespace}/{name}/stats` | Get install, download, and event statistics. Optional `days` 1–365, default 30. |
| `GET` | `/packages/{namespace}/{name}/screenshots` | List screenshots. |
| `POST` | `/packages/{namespace}/{name}/screenshots` | Upload a screenshot. Raw image body; optional `?caption=...`. |
| `DELETE` | `/packages/{namespace}/{name}/screenshots/{screenshotId}` | Delete a screenshot. |
| `POST` | `/packages/{namespace}/{name}/archive` | Archive an owned package. Optional `{"reason": "..."}`. |
| `POST` | `/packages/{namespace}/{name}/restore` | Restore an archived package. |
| `GET` | `/me/packages` | List owned packages with `q`, `status`, `visibility`, `archived`, `limit`, and `cursor`. |
| `GET` | `/namespaces` | List namespaces owned by the caller. |
| `POST` | `/namespaces` | Claim a namespace. Body: `{"namespace": "my-studio", "spaceId": "!space:example.org"}`; `spaceId` is optional. |
| `GET` | `/namespaces/{namespace}` | Get namespace ownership and package count. |
| `POST` | `/admin/namespaces/{namespace}/transfer` | Transfer a namespace and its packages. Matrix administrators only. |
| `POST` | `/packages/{namespace}/{name}/submit` | Submit an owned package for review. |
| `POST` | `/packages/{namespace}/{name}/icon` | Upload an icon. Body is a PNG, JPEG, GIF, or WebP image. |
| `DELETE` | `/packages/{namespace}/{name}/icon` | Remove the icon. |
| `POST` | `/packages/{namespace}/{name}/banner` | Upload a banner. Body is a PNG, JPEG, GIF, or WebP image. |
| `DELETE` | `/packages/{namespace}/{name}/banner` | Remove the banner. |
| `GET` | `/packages/toreview` | List pending packages. Matrix administrators only. |
| `POST` | `/packages/{namespace}/{name}/review` | Approve or reject a package. Matrix administrators only. |
| `GET` | `/admin/package-versions` | List versions awaiting review. Matrix administrators only. |
| `POST` | `/admin/packages/{namespace}/{name}/versions/{version}/review` | Approve or reject one pending version. Matrix administrators only. |
| `GET` | `/admin/packages` | List all packages with `q`, `status`, `limit`, and `cursor`. Matrix administrators only. |
| `PATCH` | `/admin/packages/{namespace}/{name}` | Edit any package field, including review state, owner, timestamps, media URLs, and the `official` marker. Matrix administrators only. |
| `DELETE` | `/admin/packages/{namespace}/{name}` | Permanently delete a package, its versions, reviews, comments, reactions, acquisitions, and archives. Matrix administrators only. |
| `GET` | `/admin/packages/{namespace}/{name}/versions` | List all versions, including yanked versions. Matrix administrators only. |
| `POST` | `/admin/packages/{namespace}/{name}/versions` | Upload a new version without changing the package's review status. Matrix administrators only. |
| `PATCH` | `/admin/packages/{namespace}/{name}/versions/{version}` | Edit version manifest, entrypoint, engine constraint, dependencies, publish time, or yank state. Matrix administrators only. |
| `DELETE` | `/admin/packages/{namespace}/{name}/versions/{version}` | Delete one version and its archive. Matrix administrators only. |
| `GET` | `/packages/{namespace}/{name}/versions/{version}` | Get version metadata. |
| `POST` | `/packages/{namespace}/{name}/versions/{version}/yank` | Yank a version. |
| `GET` | `/packages/{namespace}/{name}/versions/{version}/download` | Download a version archive and increment `downloadCount`. |

Publishing uses `multipart/mixed` with a `manifest` field and one archive file
part. Synapse rejects `multipart/form-data` before module routing, so clients
must use `multipart/mixed`. Package browsing lives in the marketplace API.

A manifest is a JSON object:

```json
{
  "name": "tool",
  "version": "1.2.0",
  "description": "A starter tool",
  "entrypoint": "src/main.luau",
  "engineConstraint": "^1.0.0",
  "type": "tool",
  "tags": ["starter", "tool"],
  "visibility": "public",
  "spaceId": "!space:example.org",
  "dependencies": {"alice/core": "^2.0.0"}
}
```

`type` is `model`, `plugin`, `template`, `tool`, or `package` and defaults to
`package`. `visibility` is `public`, `unlisted`, or `private` and defaults to
`public`. Package types and visibility can also be changed through `PATCH
/packages/{namespace}/{name}`. Private packages are visible only to their
owner, including direct package, version, download, and dependency-resolution
requests.

Archives must be ZIP or tar, must not contain absolute paths, `..`, links,
encrypted entries, or duplicate names, and must stay within the archive limits.

Packages and versions are reviewed before they appear publicly. A new package
starts in `pending`; its owner can still view and download it. A new version of
an existing approved package also starts in `pending`, but the package stays
approved and its previously approved versions remain available. A Matrix server
administrator can list pending packages and versions, then approve or reject
each one:

```json
{"decision": "reject", "note": "Please document the new behavior."}
```

Rejections require a note. Rejected packages remain owner-only until the owner
updates the package and calls `/packages/{namespace}/{name}/submit`, which
returns the package and any rejected versions to `pending`. Rejected versions
stay visible only to their owner and administrators. Package and version
responses include `reviewStatus`, `reviewNote`, `reviewedBy`, `reviewedAt`, and
`submittedAt`.

Public package responses, version listings, manifests, downloads, dependency
resolution, marketplace listings, updates, and widgets only use approved
non-yanked versions. Owners and administrators can inspect pending or rejected
versions.

Package icons and banners are uploaded after publishing through the image
endpoints above. They are stored as `mxc://` URLs and returned as `iconUrl` and
`bannerUrl`. The review page renders them alongside the package type, license,
engine constraint, visibility, repository, tags, description, owner, and
submission time.

Package responses include `spaceId`, `official`, `officialAt`, and
`officialBy`, plus `creator` (`{"userId": ..., "spaceId": ...}`), `reactions`
(`likes`, `dislikes`, `score`, and `mine`), `installCount`, and
`downloadCount`, plus readme and archive metadata. Archived packages are hidden
from public package and marketplace reads but remain visible to their owner.
`/packages/{namespace}/{name}/versions` is paginated and avoids the fixed
100-version detail limit.

Screenshots accept PNG, JPEG, GIF, and WebP up to `max_media_bytes`. Statistics
return cumulative installs and downloads plus daily event series for
`download`, `install`, `uninstall`, and `update`. Changelogs and readmes are
plain text; clients may render them as Markdown. `installCount` is the number of unique acquisitions, so it
increments once per user per package. `downloadCount` increments on every
successful call to
`/packages/{namespace}/{name}/versions/{version}/download`; the updated count
is also returned in the `X-Kinemium-Download-Count` header.

Administrators can list every package regardless of review status, search by
namespace, name, description, or owner, filter by `pending`, `approved`, or
`rejected`, and paginate with `cursor`. The admin package list includes
`latestVersion` for each package. Admin `PATCH` can edit owner, description,
license, repository, engine constraint, item type, tags, visibility,
deprecation, review status/note/reviewer, created/published/submitted/reviewed
timestamps, icon/banner URLs, and the `official` marker. Setting `official`
records the acting administrator in `officialBy` and the current time in
`officialAt`; package owners cannot set it themselves. Changing `reviewStatus` also updates reviewer
metadata; setting it to `pending` clears reviewer metadata. Admin deletion
cascades through package versions, dependencies, acquisitions, marketplace
reviews, comments, and reactions, and removes stored version archives. Namespace
ownership is not deleted, so the owner can publish a package with the same name
later.

Admin version uploads use the same `multipart/mixed` format as publishing. A
separate `version` field may be supplied alongside `manifest`; the manifest
`name` and `namespace`, when present, must match the package. Admin-added
versions are approved immediately and preserve the package's current review
status. Admins can also edit version fields, replace dependency metadata,
yank or restore versions, and delete individual versions. Package icons and
banners can be uploaded or removed by administrators through the owner image
endpoints.

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

The marketplace lists packages. There are no separate marketplace items or
item versions. Pass `namespace` to `/marketplace` to browse packages from a
single namespace, and `official=true` or `official=false` to filter by the
administrator-managed marker. Browse returns approved packages that have at least one
non-yanked version and are visible to the caller. Unlisted packages are
available by direct path; private, pending, and rejected packages are
owner-only. Paid listings are not supported.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/marketplace` | Browse public packages. Optional `q`, `namespace`, `type`, `tag`, `official`, `sort`, `cursor`, `limit`. |
| `GET` | `/marketplace/{namespace}/{name}` | Get a package, versions, rating, reactions, comments, install/download counts, and acquisition state. |
| `POST` | `/marketplace/{namespace}/{name}/acquire` | Acquire a package that has a non-yanked version. |
| `DELETE` | `/marketplace/{namespace}/{name}/acquire` | Uninstall an acquired package. |
| `PATCH` | `/marketplace/{namespace}/{name}/acquire` | Pin the acquisition to `{"version": "..."}` or `null`. |
| `GET` | `/marketplace/acquisitions` | List the current user's acquisitions. Optional `cursor`, `limit`. |
| `GET` | `/marketplace/updates` | List acquisitions and whether a different latest version is available. Optional `cursor`, `limit`. |
| `GET` | `/marketplace/{namespace}/{name}/reviews` | List reviews and the aggregate rating. |
| `POST` | `/marketplace/{namespace}/{name}/reviews` | Create or replace a review. Requires acquisition. Body: `rating` 1–5 and optional `body`. |
| `GET` | `/marketplace/{namespace}/{name}/comments` | List comments. Optional `cursor`, `limit`. |
| `POST` | `/marketplace/{namespace}/{name}/comments` | Add a comment. Body: non-empty `body`. |
| `PATCH` | `/marketplace/{namespace}/{name}/comments/{commentId}` | Edit your comment. Body: `{"body": "..."}`. |
| `DELETE` | `/marketplace/{namespace}/{name}/comments/{commentId}` | Delete your comment. Package owners and administrators may also delete comments. |
| `DELETE` | `/marketplace/{namespace}/{name}/reviews/{reviewId}` | Delete your review. Package owners and administrators may also delete reviews. |
| `POST` | `/marketplace/{namespace}/{name}/reaction` | Set a like, dislike, or clear it. Body: `value` `1`, `-1`, or `0`. |

`sort` is `recent` (default) or `name`. `type` must be one of the package
types. `tag` matches a whole tag, case-insensitively. Reviews are one per user
per package; updating a review replaces the previous rating and body. Reactions
are also one per user per package; posting `0` removes the reaction. Comments
are separate from reviews and can be added without acquiring the package.
Reaction counts, `installCount`, and `downloadCount` appear in browse, owner,
package, marketplace, and admin responses. Comments and reviews are soft-deleted
and excluded from counts and listings. Acquisitions record `pinnedVersion` and
`updatedAt`; `installCount` remains the number of unique current acquisitions.



## Platform services

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/capabilities` | Get API version, scopes, limits, and feature flags. |
| `GET` | `/openapi.json` | Get a machine-readable OpenAPI 3.1 description. |
| `POST` | `/reports` | Report a package, comment, review, or user. |
| `GET` | `/admin/reports` | List reports. Optional `status`, `limit`, `cursor`. Matrix administrators only. |
| `POST` | `/admin/reports/{reportId}/resolve` | Resolve or dismiss a report with `{"status": "resolved", "note": "..."}`. Matrix administrators only. |
| `GET` | `/admin/audit` | List administrator audit events. Optional `limit`, `cursor`. Matrix administrators only. |
| `GET` | `/me/notifications` | List notifications. Optional `unread=true`, `limit`, `cursor`. |
| `POST` | `/me/notifications/read` | Mark notifications read. Body: `{"ids": [...]}`; omit `ids` to mark all. |
| `DELETE` | `/me/notifications/{notificationId}` | Delete one notification. |
| `GET` | `/me/webhooks` | List webhooks. |
| `POST` | `/me/webhooks` | Create a webhook. Body: `{"url": "https://...", "events": ["*"]}`. |
| `PATCH` | `/me/webhooks/{webhookId}` | Update `url`, `events`, or `active`. |
| `DELETE` | `/me/webhooks/{webhookId}` | Delete a webhook. |

Reports use `targetType` `package`, `comment`, `review`, or `user`; package
content reports also need `namespace` and `name`. Reports start `open` and can
be resolved or dismissed by an administrator. Resolutions and package
moderation actions are written to the administrator audit log.

Notifications are generated for package acquisitions, uninstalls, reviews,
comments, review decisions, and namespace transfers. Webhook deliveries are
signed with HMAC-SHA256 in `X-Kinemium-Signature`, use the event name in
`X-Kinemium-Event`, and include `{"event", "createdAt", "data"}`. Webhook URLs
must be public HTTP or HTTPS URLs; loopback, private, link-local, multicast,
reserved, local hostnames, and non-public DNS results are rejected. Redirects
are not followed. The signing secret is returned only when the webhook is
created.

## Rate limits and idempotency

Rate-limited responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
`X-RateLimit-Reset`, and, when rejected, `Retry-After`. CORS responses expose
these headers to browsers.

Mutations that create resources accept an `Idempotency-Key` header. Keys are
scoped per user, method, and path for seven days. A replay returns the original
status and JSON body with `Idempotency-Replayed: true`; a new request returns
`Idempotency-Replayed: false`. Supported endpoints are package publishing,
package acquisition, comments, reviews, reports, and namespace claims.

## Review page

Server administrators can review pending packages at:

```
https://opus.quadigen.com/_kinemium/admin/review
```

For a local instance, the equivalent URL is
`http://localhost:8008/_kinemium/admin/review`.

The page signs in with a Matrix account through the normal password login API
and keeps the access token in the browser tab's session storage. The Packages
tab lists pending packages, supports approve and reject actions, and requires a
note for rejections. The Versions tab lists pending package versions and lets
administrators approve or reject each one without hiding previously approved
versions. The All packages tab lists every package with search and status
filters. Administrators can download the latest version, edit metadata and
review state inline, manage timestamps and media, upload new versions, yank or
restore versions, delete versions, or permanently delete a package after a
confirmation step. Non-administrators are signed out by the admin APIs.

## Embeddable widgets

Replace `https://opus.quadigen.com` in the examples below with your own
deployment's base URL when self-hosting.

Public widgets are available at `/_kinemium/widget` and
`/_kinemium/widget.svg`. Both accept comma-separated or repeated
`packages`/`plugins`, `users`, `namespaces`, and `games` parameters. Up to 12
entries per category and 40 entries total are rendered. Only approved,
non-archived public packages, public profiles, known namespaces, and existing
games are shown; other entries render as unavailable. `title` sets the heading.
The SVG endpoint also accepts `theme=auto|light|dark` and `width` (320–1200).

For a normal website, embed the HTML widget:

```html
<iframe
  src="https://opus.quadigen.com/_kinemium/widget?packages=devcell/part&users=%40devcell%3Aquadigen.com&namespaces=devcell&title=Kinemium"
  width="100%"
  height="420"
  style="border:0;border-radius:12px"
  title="Kinemium"
  loading="lazy"
></iframe>
```

GitHub sanitizes iframes out of repository README files, so GitHub READMEs
must use the SVG endpoint as an image instead:

```markdown
![Kinemium widget](https://opus.quadigen.com/_kinemium/widget.svg?packages=devcell/part&users=%40devcell%3Aquadigen.com&namespaces=devcell&theme=auto)
```

For a light/dark GitHub theme pair:

```html
<picture>
  <source media="(prefers-color-scheme: dark)"
    srcset="https://opus.quadigen.com/_kinemium/widget.svg?packages=devcell/part&theme=dark">
  <img alt="Kinemium widget"
    src="https://opus.quadigen.com/_kinemium/widget.svg?packages=devcell/part&theme=light">
</picture>
```

The HTML widget posts a `kinemium-widget-height` message to its parent after
layout so embedding pages can resize the iframe automatically.

## Storage and limits

Uploaded archives are written atomically under `storage_path` with a
server-generated key. Downloads are served with the original sanitized filename
and a SHA-256 digest is stored in the database. Database failures remove newly
written archives; if a later step fails after a package row is created, the
package row may remain without the failed version.

Rate limits are applied in memory per worker. A multi-worker deployment applies
limits independently in each worker. Idempotency records, notifications,
reports, audit events, and webhook deliveries are stored in the database.

API responses set `X-Content-Type-Options: nosniff`, `Referrer-Policy:
no-referrer`, `X-Frame-Options: DENY`, and `Cache-Control: no-store`.
Downloads are also checked against their stored SHA-256 digest before being
served. When CORS is enabled, `cors_origins` defaults to `*` for compatibility;
production deployments should set it to the exact browser origins that need
access.

## Tests

The integration tests use the Synapse test harness and cover accounts, games,
spaces, package lifecycle, marketplace moderation, reports, notifications,
webhooks, audit logging, idempotency, game-server hosting, and Studio
collaboration:

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
