Opus
====

**The backend platform for Kinemium.**

Opus is the self-hostable online backend for Kinemium. It is a Synapse-based
`Matrix <https://matrix.org>`__ homeserver with the Kinemium developer-platform
module built in under ``/_kinemium/v1``. It provides the accounts, identities,
packages, marketplace, spaces, hosting, collaboration, and platform services
that Kinemium clients use around games and packages.

Opus is not the Kinemium engine or editor. Kinemium still runs games locally and
exports standalone builds. Opus supplies the online services that connect
creators, packages, spaces, and playtests.

Because Opus is built on `Synapse <https://github.com/element-hq/synapse>`__, it
also provides normal Matrix accounts, rooms, client APIs, and federation.
Kinemium-specific behavior lives in the module.

What Opus provides
------------------

Accounts and identity
~~~~~~~~~~~~~~~~~~~~~

* Matrix accounts for Kinemium users.
* Developer profiles with display names, bios, websites, pronouns, status text,
  status emoji, accent colors, avatars, and banners.
* Public developer profiles and account pages.
* Game identities with names, descriptions, websites, icons, and banners.
* Public game profiles that other Kinemium users and widgets can display.

Authentication and API access
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Standard Matrix access tokens.
* Kinemium API tokens prefixed with ``knm_``.
* Scoped API tokens for account, package, marketplace, and space operations.
* Game-bound tokens that can read and update one game without managing the
  developer's other games or tokens.
* Token listing, revocation, and scope enforcement.

Package registry
~~~~~~~~~~~~~~~~

* Namespaces for developers and Matrix spaces.
* Publishing of Kinemium package archives in ZIP or tar format.
* Versioned manifests with entrypoints, engine constraints, dependencies, tags,
  visibility, and package types.
* Readmes, changelogs, screenshots, icons, banners, and download statistics.
* Dependency resolution and package version pinning.
* Version yanking, package archiving, review queues, and download integrity
  checks using SHA-256 digests.
* Owner and administrator management of packages and versions.

Marketplace
~~~~~~~~~~~

* A package-backed marketplace for approved Kinemium packages.
* Search and filtering by namespace, type, tag, and official status.
* Acquire, uninstall, pin, and update workflows.
* Ratings, reviews, comments, reactions, and install/download counts.
* Package and version review before public listing.
* Moderation reports and administrator actions. Paid listings are not supported.

Spaces
~~~~~~

* Matrix spaces with Kinemium profiles.
* Space names, topics, descriptions, avatars, banners, and accent colors.
* Custom space emojis, including animated GIF emojis.
* Space-owned package namespaces and packages.
* Administrator-managed official package markers.

Game server hosting
~~~~~~~~~~~~~~~~~~~

* Free hosted-server requests for Kinemium games.
* ``.kine`` project uploads with region, player limit, and operator notes.
* A provisioning queue for an operator-managed worker.
* Server records, addresses, ports, heartbeats, and status tracking.
* Administrator views for pending requests and provisioned servers.

Studio collaboration
~~~~~~~~~~~~~~~~~~~~

* Temporary collaboration sessions for a game.
* Invitations, join codes, join requests, approvals, denials, and participant
  management.
* WebRTC signaling for editor sessions over HTTP polling.
* Session, invitation, and signal expiry with host heartbeats.
* Peer-to-peer project synchronization. Opus does not store project data.

Platform services
~~~~~~~~~~~~~~~~~

* Runtime capability discovery at ``/_kinemium/v1/capabilities``.
* A machine-readable OpenAPI 3.1 description at ``/_kinemium/v1/openapi.json``.
* In-app notifications for package, marketplace, namespace, and collaboration
  activity.
* HMAC-signed webhooks for platform events.
* Reports, administrator audit logs, rate limits, idempotency keys, CORS
  controls, and security headers.
* Embeddable HTML and SVG widgets for packages, users, namespaces, and games.

Administration
~~~~~~~~~~~~~~

* A browser review page at ``/_kinemium/admin/review``.
* Package and version approval or rejection with review notes.
* Full package search, editing, version uploads, yanking, restoring, and
  deletion.
* Namespace transfers, moderation reports, audit history, and game-server
  administration.

Architecture
------------

.. code-block:: text

   Kinemium client or editor
             |
             | Matrix access token or knm_ API token
             v
   Opus  /_kinemium/v1
             |
             +-- Kinemium module services
             +-- Synapse: accounts, rooms, auth, federation
             +-- PostgreSQL or SQLite
             +-- storage_path: package archives

The module stores its data in the normal Synapse database. Schema deltas 95
through 106 add the Kinemium tables. Uploaded package archives are stored under
``storage_path``; only the local storage backend is supported.

Quick start
-----------

1. Install the repository and its Rust extension:

   .. code-block:: bash

      git clone https://github.com/quadigen/opus.git
      cd opus
      python3 -m venv .venv
      .venv/bin/pip install -e .

2. Add the module to ``homeserver.yaml``:

   .. code-block:: yaml

      modules:
        - module: kinemium_module.KinemiumModule
          config:
            storage_path: /var/lib/synapse/kinemium

3. Apply the normal Synapse schema migration and start Synapse.

``storage_path`` is required and must be an absolute, writable directory.
PostgreSQL is recommended for production; SQLite is supported. See the
`local hosting guide <docs/kinemium-local-hosting.md>`__ for a complete setup,
including the Opus client, CORS, HTTPS, and reverse-proxy notes.

Authentication
--------------

Requests may use a Matrix access token or a Kinemium API token:

.. code-block:: http

   Authorization: Bearer <matrix_access_token>
   Authorization: Bearer knm_<token>

Kinemium tokens are created with ``POST /me/tokens`` and are shown only once.
They must be sent in the ``Authorization`` header, not as an ``access_token``
query parameter. Matrix tokens have all scopes. API tokens receive only the
scopes requested when they are created.

Documentation
-------------

* `Kinemium API reference <docs/kinemium.md>`__: endpoints, schemas, scopes,
  review behavior, collaboration, widgets, and limits.
* `Local hosting guide <docs/kinemium-local-hosting.md>`__: running Opus and
  the Opus client on your own machine or server.
* ``/_kinemium/v1/capabilities``: runtime feature flags and limits.
* ``/_kinemium/v1/openapi.json``: machine-readable API description.

Development
-----------

Run the Kinemium test suite:

.. code-block:: bash

   .venv/bin/python -m twisted.trial tests.kinemium.test_kinemium

The suite covers accounts, games, spaces, package lifecycle, marketplace
moderation, reports, notifications, webhooks, audit logging, idempotency,
game-server hosting, and Studio collaboration.

When Rust sources change, rebuild the extension:

.. code-block:: bash

   uv pip install --python .venv/bin/python -e .

Security notes
--------------

* Keep ``homeserver.yaml``, signing keys, database files, and API keys out of
  Git.
* Use HTTPS and email verification for public registration.
* Set ``cors_origins`` to the exact browser origins used in production instead
  of the permissive default.
* Treat collaboration join codes and webhook signing secrets as credentials.

Upstream and licensing
----------------------

Opus is a fork of Synapse. Upstream Synapse documentation still applies to
Matrix homeserver administration, federation, and deployment.

Synapse is dual-licensed by Element Creations Ltd under the GNU Affero General
Public License v3 or a commercial license. See ``LICENSE-AGPL-3.0`` and
``LICENSE-COMMERCIAL`` for the full terms. Review the repository license files
before redistributing Opus.

Copyright and Licensing
-----------------------

  | Copyright 2014-2017 OpenMarket Ltd
  | Copyright 2017 Vector Creations Ltd
  | Copyright 2017-2025 New Vector Ltd
  | Copyright 2025 Element Creations Ltd

This software is dual-licensed by Element Creations Ltd (Element). It can be
used either:

(1) for free under the terms of the GNU Affero General Public License (as
    published by the Free Software Foundation, either version 3 of the License,
    or (at your option) any later version); OR

(2) under the terms of a paid-for Element Commercial License agreement between
    you and Element (the terms of which may vary depending on what you and
    Element have agreed to).

Unless required by applicable law or agreed to in writing, software distributed
under the Licenses is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the Licenses for the
specific language governing permissions and limitations under the Licenses.

Please contact `licensing@element.io <mailto:licensing@element.io>`_ to purchase
an Element commercial license for this software.
