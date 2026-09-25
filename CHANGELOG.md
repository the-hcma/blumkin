# Changelog

## [1.9.1](https://github.com/the-hcma/blumkin/compare/blumkin-v1.9.0...blumkin-v1.9.1) (2026-09-25)


### Bug Fixes

* skip re-uploading attachments already on a draft ([#392](https://github.com/the-hcma/blumkin/issues/392)) ([#394](https://github.com/the-hcma/blumkin/issues/394)) ([d6abc20](https://github.com/the-hcma/blumkin/commit/d6abc2073f37f526798915628d2f2dcd526bc7ac))

## [1.9.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.8.1...blumkin-v1.9.0) (2026-09-25)


### Features

* prescriptive next-step guidance in doctor and auth status ([#368](https://github.com/the-hcma/blumkin/issues/368)) ([#391](https://github.com/the-hcma/blumkin/issues/391)) ([84e280f](https://github.com/the-hcma/blumkin/commit/84e280f6145470c92308381c544eb2144f10dfa1))

## [1.8.1](https://github.com/the-hcma/blumkin/compare/blumkin-v1.8.0...blumkin-v1.8.1) (2026-09-25)


### Bug Fixes

* restore microsoft-kiota-http upper bound (&lt;1.13.0) ([#388](https://github.com/the-hcma/blumkin/issues/388)) ([1da85ae](https://github.com/the-hcma/blumkin/commit/1da85aeca46ef36593ff2f652c4ba9ed341eb891))

## [1.8.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.7.0...blumkin-v1.8.0) (2026-09-24)


### Features

* **config:** centralize Google OAuth non-secret client fields in config.toml ([#378](https://github.com/the-hcma/blumkin/issues/378)) ([b8ae9b0](https://github.com/the-hcma/blumkin/commit/b8ae9b0018daa9c16cf699a42789fe4d08256506))
* enforce a confirm cooldown before mail.send-draft ([#365](https://github.com/the-hcma/blumkin/issues/365)) ([#366](https://github.com/the-hcma/blumkin/issues/366)) ([c63ad9f](https://github.com/the-hcma/blumkin/commit/c63ad9fd56724f9d3bfe7f7334bc49240799544e))
* gate calendar RSVP/cancel on a fresh calendar.get (issue [#365](https://github.com/the-hcma/blumkin/issues/365)) ([#372](https://github.com/the-hcma/blumkin/issues/372)) ([ebc6f79](https://github.com/the-hcma/blumkin/commit/ebc6f798ffe3a47e439f046b45dd5a7502269988))
* guided OAuth app-registration setup ([#368](https://github.com/the-hcma/blumkin/issues/368)) ([#383](https://github.com/the-hcma/blumkin/issues/383)) ([f84033e](https://github.com/the-hcma/blumkin/commit/f84033e9e759dd7221a815e48648a35c8c971f09))
* **mail:** defer send-draft delivery and add mail.cancel-send ([#365](https://github.com/the-hcma/blumkin/issues/365)) ([#373](https://github.com/the-hcma/blumkin/issues/373)) ([2e2b12f](https://github.com/the-hcma/blumkin/commit/2e2b12f1ba5e6ac707f99c596b35d04d40a2f4c1))
* split chat send/edit into compose/emit skills (issue [#365](https://github.com/the-hcma/blumkin/issues/365)) ([#370](https://github.com/the-hcma/blumkin/issues/370)) ([f302af9](https://github.com/the-hcma/blumkin/commit/f302af9a04c9100d26d3c8aeac3d44e278bf5aa1))
* vault Google client_secret and MS client_id in the OS keychain ([#368](https://github.com/the-hcma/blumkin/issues/368)) ([#379](https://github.com/the-hcma/blumkin/issues/379)) ([eae80d7](https://github.com/the-hcma/blumkin/commit/eae80d72db624a015243c72e642e3ca3bc969a75))


### Bug Fixes

* clean up vaulted app secrets on blumkin uninstall ([#380](https://github.com/the-hcma/blumkin/issues/380)) ([c3a3c2e](https://github.com/the-hcma/blumkin/commit/c3a3c2e289d4a4652d7d3e29e8c1afeb90297107))
* surface a double-failure (write + read) that shadows a stale ms_client_id ([#384](https://github.com/the-hcma/blumkin/issues/384)) ([#385](https://github.com/the-hcma/blumkin/issues/385)) ([1111b63](https://github.com/the-hcma/blumkin/commit/1111b630f558c521c99f76ba9e608826c8237bcb))
* use vaulted ms_client_id when reading granted scopes ([#368](https://github.com/the-hcma/blumkin/issues/368)) ([#381](https://github.com/the-hcma/blumkin/issues/381)) ([e348ecb](https://github.com/the-hcma/blumkin/commit/e348ecb13cd8886ab58617593852f392d9aff708))


### Documentation

* add personal Microsoft account (MSA) setup guide ([#369](https://github.com/the-hcma/blumkin/issues/369)) ([#374](https://github.com/the-hcma/blumkin/issues/374)) ([bcba786](https://github.com/the-hcma/blumkin/commit/bcba7867d736d736fd05d299aaa26b7304769dbd))
* **agents:** fix github-content-formatting rule scope and rh init ([#376](https://github.com/the-hcma/blumkin/issues/376)) ([8a5d96f](https://github.com/the-hcma/blumkin/commit/8a5d96f68f47519ddec405239b9df6a7525e3b5a))
* **agents:** sync github-content-formatting rule from repository-helpers ([#375](https://github.com/the-hcma/blumkin/issues/375)) ([8992cf3](https://github.com/the-hcma/blumkin/commit/8992cf39ce015f57e8b026b2f7d2c822bc3202a2))
* **agents:** sync pr-ship-and-review rule from repository-helpers ([#377](https://github.com/the-hcma/blumkin/issues/377)) ([97c963c](https://github.com/the-hcma/blumkin/commit/97c963cbb8f8b721917eca247ee1214f06ff08c5))

## [1.7.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.6.0...blumkin-v1.7.0) (2026-09-21)


### Features

* support personal Microsoft accounts via account_type profile setting ([#362](https://github.com/the-hcma/blumkin/issues/362)) ([a8a16b0](https://github.com/the-hcma/blumkin/commit/a8a16b0919377ef2c1d52e516073e80ba21b1249))


### Documentation

* add blumkin brand icon and link Rose Blumkin's Wikipedia page ([#361](https://github.com/the-hcma/blumkin/issues/361)) ([c53e006](https://github.com/the-hcma/blumkin/commit/c53e006e309d683ca3a04c84ece31cf85b50d623))

## [1.6.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.5.0...blumkin-v1.6.0) (2026-09-21)


### Features

* add uninstall command with per-category teardown ([#344](https://github.com/the-hcma/blumkin/issues/344)) ([#347](https://github.com/the-hcma/blumkin/issues/347)) ([426b957](https://github.com/the-hcma/blumkin/commit/426b9576db7711015e9ac54a44df97aea65ed080))
* **agent:** add LocalAuthentication + TTL + mlock secret cache core ([#340](https://github.com/the-hcma/blumkin/issues/340)) ([9b346c3](https://github.com/the-hcma/blumkin/commit/9b346c3bed034e57c7a0b48c175089962d6052ba))
* **agent:** wire unlock/get_secret protocol commands to the secret cache ([#341](https://github.com/the-hcma/blumkin/issues/341)) ([cdc18cc](https://github.com/the-hcma/blumkin/commit/cdc18cc364db19b465d622daa0b7f21b35475873))
* cache decrypted MS/Google credentials in blumkin-agent ([#339](https://github.com/the-hcma/blumkin/issues/339)) ([#346](https://github.com/the-hcma/blumkin/issues/346)) ([3503ae9](https://github.com/the-hcma/blumkin/commit/3503ae90e01ca7c6a9ffdf133a29f8a2617cd0b8))


### Bug Fixes

* honor --no-&lt;category&gt; flags in uninstall --dry-run preview ([#352](https://github.com/the-hcma/blumkin/issues/352)) ([3f4c2be](https://github.com/the-hcma/blumkin/commit/3f4c2be04651b418bbb09fcdc4d485dd84e87870))
* pin microsoft-kiota-http below the msgraph-core break ([#357](https://github.com/the-hcma/blumkin/issues/357)) ([407c7e3](https://github.com/the-hcma/blumkin/commit/407c7e3b0fe3a27d14386aa96c3300bf125974bd))
* report keyring-backed storage accurately in auth login message ([#351](https://github.com/the-hcma/blumkin/issues/351)) ([db38148](https://github.com/the-hcma/blumkin/commit/db3814891a8d7eb8db29eff60432f9e2015c967c))
* stop concurrent unlock presence checks from canceling each other ([#343](https://github.com/the-hcma/blumkin/issues/343)) ([#345](https://github.com/the-hcma/blumkin/issues/345)) ([ad0114c](https://github.com/the-hcma/blumkin/commit/ad0114c1f45d651405b38cd58ad0f40ee7a90944))

## [1.5.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.4.0...blumkin-v1.5.0) (2026-09-19)


### Features

* **agent:** add blumkin-agent daemon foundation (issue [#328](https://github.com/the-hcma/blumkin/issues/328)) ([#329](https://github.com/the-hcma/blumkin/issues/329)) ([cd28f29](https://github.com/the-hcma/blumkin/commit/cd28f2951f0ba44b78364750bb5300dd48c2d9f3))
* **release:** publish a macOS wheel bundling blumkin-agent (issue [#328](https://github.com/the-hcma/blumkin/issues/328)) ([#331](https://github.com/the-hcma/blumkin/issues/331)) ([b872faf](https://github.com/the-hcma/blumkin/commit/b872faf47bd79c01bfc19bd9abe9a7f2a8fa2577))


### Continuous Integration

* **agent:** lint and test rust-agent/ (issue [#328](https://github.com/the-hcma/blumkin/issues/328)) ([#330](https://github.com/the-hcma/blumkin/issues/330)) ([9b4c33a](https://github.com/the-hcma/blumkin/commit/9b4c33a))


### Documentation

* document the Rust agent exception and its install requirement ([#332](https://github.com/the-hcma/blumkin/issues/332)) ([9c10c66](https://github.com/the-hcma/blumkin/commit/9c10c666738005df28d906b13e463b5d788ec10a))

## [1.4.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.3.0...blumkin-v1.4.0) (2026-09-18)


### Features

* **auth:** enrich auth status --json with account and capabilities ([#326](https://github.com/the-hcma/blumkin/issues/326)) ([14f6404](https://github.com/the-hcma/blumkin/commit/14f6404fa0c546fbecc7a93eeabc70109e330041))
* **auth:** merge auth_record and token_cache into one keychain item ([#319](https://github.com/the-hcma/blumkin/issues/319)) ([1031078](https://github.com/the-hcma/blumkin/commit/103107807a6a29cff6fdaba2607ed6dc4b70c5ef))
* **cli:** add blumkin capabilities --json command ([#325](https://github.com/the-hcma/blumkin/issues/325)) ([ed46874](https://github.com/the-hcma/blumkin/commit/ed4687471d54e8644d09b399990e7b30f28e58a8))
* **doctor:** report per-family capability summary ([#324](https://github.com/the-hcma/blumkin/issues/324)) ([cc4da48](https://github.com/the-hcma/blumkin/commit/cc4da483dc1e1deea5d4005e211605386a4b9b64))
* **mail:** add actionable hints for known wrong-id/wrong-folder errors ([#314](https://github.com/the-hcma/blumkin/issues/314)) ([#322](https://github.com/the-hcma/blumkin/issues/322)) ([870614c](https://github.com/the-hcma/blumkin/commit/870614c01d52d2d76efee37257f6e10bffde1990))


### Bug Fixes

* **mail:** scope mail inbox to the Inbox folder ([#309](https://github.com/the-hcma/blumkin/issues/309)) ([#310](https://github.com/the-hcma/blumkin/issues/310)) ([72e65ac](https://github.com/the-hcma/blumkin/commit/72e65ac77b77c1086c441c7d64f4a72c8d1fc643))


### Documentation

* add CLI epilog convention rule ([#312](https://github.com/the-hcma/blumkin/issues/312)) ([#321](https://github.com/the-hcma/blumkin/issues/321)) ([b93fda9](https://github.com/the-hcma/blumkin/commit/b93fda9714ef63b32be46066bd590c6324652906))

## [1.3.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.2.1...blumkin-v1.3.0) (2026-09-17)


### Features

* make blumkin's skill discoverable by pi.dev ([#299](https://github.com/the-hcma/blumkin/issues/299)) ([#301](https://github.com/the-hcma/blumkin/issues/301)) ([9f3338d](https://github.com/the-hcma/blumkin/commit/9f3338d6d426d05b48f2d0fa1a418a56cf7f4f0f))
* **release:** automate npm publishing for the pi.dev skill package ([#303](https://github.com/the-hcma/blumkin/issues/303)) ([736977b](https://github.com/the-hcma/blumkin/commit/736977b78d8fa3d8ca188ef732d1e5fd6486faae))
* store token cache/auth record in OS keychain when available ([#289](https://github.com/the-hcma/blumkin/issues/289)) ([5d45184](https://github.com/the-hcma/blumkin/commit/5d45184a68c29575d08b69cc878d75b91a83887e))


### Bug Fixes

* **mail:** add a reflowed html alternative to Gmail body_type=text sends ([#305](https://github.com/the-hcma/blumkin/issues/305)) ([01907d1](https://github.com/the-hcma/blumkin/commit/01907d1f6b6abe14ffcd6ae07b2f2f711eb90a34))
* make list_profiles() tolerate a single profile's config errors ([#295](https://github.com/the-hcma/blumkin/issues/295)) ([40fc7cc](https://github.com/the-hcma/blumkin/commit/40fc7cc1352da5cf6068949336af3b8afd9b082e))


### Documentation

* add Microsoft app registration hardening checklist ([#296](https://github.com/the-hcma/blumkin/issues/296)) ([db38242](https://github.com/the-hcma/blumkin/commit/db382428f84831d822fbc136e3c8f37eb0a820ca))

## [1.2.1](https://github.com/the-hcma/blumkin/compare/blumkin-v1.2.0...blumkin-v1.2.1) (2026-09-15)


### Bug Fixes

* retry mail list/search/thread without meetingMessageType on a plain 400 ([#291](https://github.com/the-hcma/blumkin/issues/291)) ([e11e699](https://github.com/the-hcma/blumkin/commit/e11e699c8a931cc34624a52e2ba60607b52c27bc))

## [1.2.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.1.2...blumkin-v1.2.0) (2026-09-15)


### Features

* add docs read skill for local PDF/DOCX/XLSX extraction ([#279](https://github.com/the-hcma/blumkin/issues/279)) ([38fe3c2](https://github.com/the-hcma/blumkin/commit/38fe3c2fb0625ef6f6d70bdbd6b4d95d7faf1110))
* add heuristic prompt-injection detection to docs read and mail ([#284](https://github.com/the-hcma/blumkin/issues/284)) ([f87dc4b](https://github.com/the-hcma/blumkin/commit/f87dc4b54f3507fc91fe419c895cbd3904047c06))
* add image OCR support to docs read (closes [#281](https://github.com/the-hcma/blumkin/issues/281)) ([#283](https://github.com/the-hcma/blumkin/issues/283)) ([b733623](https://github.com/the-hcma/blumkin/commit/b733623a1c12ab44d98aef76e7cc3194b8a53838))


### Bug Fixes

* expose meeting-invite metadata on mail reads ([#278](https://github.com/the-hcma/blumkin/issues/278)) ([cbe9222](https://github.com/the-hcma/blumkin/commit/cbe9222ff4ad9e2656d6bc2015baec8c679d912e))


### Documentation

* clean up outdated PLAN.md and HANDOFF.md content ([#272](https://github.com/the-hcma/blumkin/issues/272)) ([9283c92](https://github.com/the-hcma/blumkin/commit/9283c92161e39b216d933b16ddad9bc739c2acd2))

## [1.1.2](https://github.com/the-hcma/blumkin/compare/blumkin-v1.1.1...blumkin-v1.1.2) (2026-09-12)


### Bug Fixes

* **docs:** actually indent a block quote's first line (follow-up to [#264](https://github.com/the-hcma/blumkin/issues/264)) ([#270](https://github.com/the-hcma/blumkin/issues/270)) ([82c8c08](https://github.com/the-hcma/blumkin/commit/82c8c08aa1441a6699e58c8afd7372a1ac9c5df4))

## [1.1.1](https://github.com/the-hcma/blumkin/compare/blumkin-v1.1.0...blumkin-v1.1.1) (2026-09-12)


### Bug Fixes

* **docs:** replace docs_update body correctly instead of appending ([#265](https://github.com/the-hcma/blumkin/issues/265)) ([9f86104](https://github.com/the-hcma/blumkin/commit/9f861041abcae6aba055e7ab145c5585f53e1fb2))
* **docs:** support block quotes and decode HTML entities in docs create ([#266](https://github.com/the-hcma/blumkin/issues/266)) ([a22ef6d](https://github.com/the-hcma/blumkin/commit/a22ef6d8ce13b4ff38fb1cb02ceabf194c1f783a))


### Documentation

* attribute the MIT license to the copyright holder in the README ([#267](https://github.com/the-hcma/blumkin/issues/267)) ([a36c7cc](https://github.com/the-hcma/blumkin/commit/a36c7ccc59ef4b299b9df92aae7739a9e4be5d4b))

## [1.1.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.0.0...blumkin-v1.1.0) (2026-09-11)


### Features

* **dispatch:** add --fields selector and default body_preview cap ([#259](https://github.com/the-hcma/blumkin/issues/259)) ([ffcf16d](https://github.com/the-hcma/blumkin/commit/ffcf16d9319a0006bba2b2d38abda70ecc15082f))
* **mail:** add client_appends_signature manual override ([#258](https://github.com/the-hcma/blumkin/issues/258)) ([e20520e](https://github.com/the-hcma/blumkin/commit/e20520e6c432d559d819301032eeccc23a17ed50))


### Bug Fixes

* **mail:** mark --attach repeatable so MCP clients get an array schema ([#251](https://github.com/the-hcma/blumkin/issues/251)) ([086b493](https://github.com/the-hcma/blumkin/commit/086b493a3611b178710fa55209195cd3a3de55f9))
* **mail:** un-escape HTML entities in --subject ([#252](https://github.com/the-hcma/blumkin/issues/252)) ([048f406](https://github.com/the-hcma/blumkin/commit/048f406d66af6426e8fe172abbc0b3804d6c554a))

## [1.0.0](https://github.com/the-hcma/blumkin/compare/blumkin-v0.7.0...blumkin-v1.0.0) (2026-09-11)


### ⚠ BREAKING CHANGES

* **config:** drop legacy flat config.toml layout ([#246](https://github.com/the-hcma/blumkin/issues/246))

### Features

* **config:** add font/HTML-email preferences with conflict warning ([#247](https://github.com/the-hcma/blumkin/issues/247)) ([f8749d5](https://github.com/the-hcma/blumkin/commit/f8749d52dc27d362d12fa011ae228bf6ff2bdcc8))
* **config:** drop legacy flat config.toml layout ([#246](https://github.com/the-hcma/blumkin/issues/246)) ([fb5a318](https://github.com/the-hcma/blumkin/commit/fb5a3182ebace69927c7985f1212842d8f4d98ea))

## [0.7.0](https://github.com/the-hcma/blumkin/compare/blumkin-v0.6.0...blumkin-v0.7.0) (2026-09-10)


### Features

* **cli:** make `upgrade` install-method aware (pipx / uv tool / editable) ([#240](https://github.com/the-hcma/blumkin/issues/240)) ([16fd715](https://github.com/the-hcma/blumkin/commit/16fd715db7c07bc96dc1392bcba98bffaa84789e))
* **people:** people.context reads ~/.config/blumkin/email-context.md ([#207](https://github.com/the-hcma/blumkin/issues/207)) ([#241](https://github.com/the-hcma/blumkin/issues/241)) ([dc9a2b4](https://github.com/the-hcma/blumkin/commit/dc9a2b4ab4f5fc686c1988eb94f34c2a41147c30))
* **tasks:** tasks.list / tasks.show read ~/.config/blumkin/tasks/*.md ([#209](https://github.com/the-hcma/blumkin/issues/209)) ([#242](https://github.com/the-hcma/blumkin/issues/242)) ([4780c68](https://github.com/the-hcma/blumkin/commit/4780c68085bae004b559e7469bf4f5066f8d316e))

## [0.6.0](https://github.com/the-hcma/blumkin/compare/blumkin-v0.5.0...blumkin-v0.6.0) (2026-09-10)


### Features

* **cli:** render long URLs as OSC 8 terminal hyperlinks ([#233](https://github.com/the-hcma/blumkin/issues/233)) ([#234](https://github.com/the-hcma/blumkin/issues/234)) ([bd38cd4](https://github.com/the-hcma/blumkin/commit/bd38cd434625bec0f00f60428ecd140c79bbb4fe))
* **mcp:** per-call `profile` argument to choose the blumkin account ([#227](https://github.com/the-hcma/blumkin/issues/227)) ([#235](https://github.com/the-hcma/blumkin/issues/235)) ([4a9ea60](https://github.com/the-hcma/blumkin/commit/4a9ea600f907e9a1b110685b7f50eee7c32c278f))

## [0.5.0](https://github.com/the-hcma/blumkin/compare/blumkin-v0.4.0...blumkin-v0.5.0) (2026-09-09)


### Features

* **docs:** docs create --folder targets pre-existing folders ([#212](https://github.com/the-hcma/blumkin/issues/212)) ([#224](https://github.com/the-hcma/blumkin/issues/224)) ([10dd675](https://github.com/the-hcma/blumkin/commit/10dd67553c2a03fc3d755bca25968a787d180e76))
* **docs:** docs update re-renders a blumkin-created doc in place ([#211](https://github.com/the-hcma/blumkin/issues/211)) ([#228](https://github.com/the-hcma/blumkin/issues/228)) ([123a436](https://github.com/the-hcma/blumkin/commit/123a436700f7c6ee3ab5c19a7d9247cf3e81931a))
* **drive:** drive download / export / read ([#208](https://github.com/the-hcma/blumkin/issues/208)) ([#222](https://github.com/the-hcma/blumkin/issues/222)) ([74d9832](https://github.com/the-hcma/blumkin/commit/74d98328414393e09e69a1f1d5ed58e91da0ead7))
* **drive:** drive list / get read side + scope plumbing ([#208](https://github.com/the-hcma/blumkin/issues/208)) ([#221](https://github.com/the-hcma/blumkin/issues/221)) ([22f5789](https://github.com/the-hcma/blumkin/commit/22f57894daf3d31ccd3f5305e837ff6de9074ac0))
* **drive:** drive mkdir / move / rename organize verbs ([#212](https://github.com/the-hcma/blumkin/issues/212)) ([#223](https://github.com/the-hcma/blumkin/issues/223)) ([f24dff3](https://github.com/the-hcma/blumkin/commit/f24dff35098c7fd4d012714022e26f80381dfa59))
* **mail:** author bodies as Markdown by default, render to HTML on the wire ([#217](https://github.com/the-hcma/blumkin/issues/217)) ([f919f55](https://github.com/the-hcma/blumkin/commit/f919f55af48b03b0c8825a89386e6bfff11b18b7))
* **mail:** detect Outlook's own auto-signature and stop double-signing ([#219](https://github.com/the-hcma/blumkin/issues/219)) ([#229](https://github.com/the-hcma/blumkin/issues/229)) ([0a108b8](https://github.com/the-hcma/blumkin/commit/0a108b86caeae53da8368a113030ef73835e23f4))


### Bug Fixes

* **mail:** keep line breaks in a Microsoft auto-reply message ([#226](https://github.com/the-hcma/blumkin/issues/226)) ([50f214c](https://github.com/the-hcma/blumkin/commit/50f214c713d4bdfcdb76876826b8455794983a42))
* **mail:** serialize drafts with CRLF so line breaks survive send ([#216](https://github.com/the-hcma/blumkin/issues/216)) ([4a49956](https://github.com/the-hcma/blumkin/commit/4a499568eeaa8f132e6fda94b4ff7d63d78bd9d4))


### Documentation

* **rules:** adopt dedicated github-api-throttle rule (repository-helpers[#608](https://github.com/the-hcma/blumkin/issues/608)) ([#220](https://github.com/the-hcma/blumkin/issues/220)) ([cc023b3](https://github.com/the-hcma/blumkin/commit/cc023b323e9acf2541ef6b9c80ac917a75be4980))
* **rules:** route ad-hoc gh through scripts/gh-api (repository-helpers[#608](https://github.com/the-hcma/blumkin/issues/608)) ([#214](https://github.com/the-hcma/blumkin/issues/214)) ([271d5ae](https://github.com/the-hcma/blumkin/commit/271d5ae3ea88fdcd55852593e333e402fb88a6ce))

## [0.4.0](https://github.com/the-hcma/blumkin/compare/blumkin-v0.3.0...blumkin-v0.4.0) (2026-09-07)


### Features

* **docs:** docs create on Google - author a Doc from a Markdown subset ([#202](https://github.com/the-hcma/blumkin/issues/202)) ([b91493b](https://github.com/the-hcma/blumkin/commit/b91493b9da37a13684e47cfa70aa142a5f263166))
* **docs:** docs create on Microsoft - .docx via python-docx + Graph upload ([#203](https://github.com/the-hcma/blumkin/issues/203)) ([32b4a90](https://github.com/the-hcma/blumkin/commit/32b4a906bc4ff096df19bed1684c31cf17ce48d0))


### Bug Fixes

* **ci:** retry the verify-pypi-release install step past CDN propagation skew ([#201](https://github.com/the-hcma/blumkin/issues/201)) ([59c12f9](https://github.com/the-hcma/blumkin/commit/59c12f94df840dfcb7b417f6d55bc9f07dcfbd01))
* **mcp:** treat an empty mcp.json placeholder as {}, not malformed ([#200](https://github.com/the-hcma/blumkin/issues/200)) ([2e1a946](https://github.com/the-hcma/blumkin/commit/2e1a9465091aeed69a98e391c9a7fbf1ebf43c1e))

## [0.3.0](https://github.com/the-hcma/blumkin/compare/blumkin-v0.2.1...blumkin-v0.3.0) (2026-09-06)


### Features

* **calendar:** calendar create --body / --location / --all-day / --optional ([#175](https://github.com/the-hcma/blumkin/issues/175)) ([#181](https://github.com/the-hcma/blumkin/issues/181)) ([8c2b922](https://github.com/the-hcma/blumkin/commit/8c2b92210ef0613a6f5f15693623698891887b23))
* **calendar:** calendar decline + calendar tentative — RSVP no / maybe ([#174](https://github.com/the-hcma/blumkin/issues/174)) ([#185](https://github.com/the-hcma/blumkin/issues/185)) ([3616543](https://github.com/the-hcma/blumkin/commit/36165432759151a6d8ad33c5dac2e530dc0fd279))
* **calendar:** calendar get - read one event in full ([#173](https://github.com/the-hcma/blumkin/issues/173)) ([#180](https://github.com/the-hcma/blumkin/issues/180)) ([3834b02](https://github.com/the-hcma/blumkin/commit/3834b0258f99e9ae9028a8c3cfa58c25afb6d9c2))
* **calendar:** calendar list + --calendar targeting ([#176](https://github.com/the-hcma/blumkin/issues/176)) ([#186](https://github.com/the-hcma/blumkin/issues/186)) ([367e863](https://github.com/the-hcma/blumkin/commit/367e863d15333d9f1c13b867a117c3c8b98f3878))
* **calendar:** calendar update - edit event fields, not just attach Teams ([#172](https://github.com/the-hcma/blumkin/issues/172)) ([#183](https://github.com/the-hcma/blumkin/issues/183)) ([8bafd0d](https://github.com/the-hcma/blumkin/commit/8bafd0d78b0bf606ce570fbcef38e23f8d0906f9))
* **completion:** add `completion <shell> --install` ([#167](https://github.com/the-hcma/blumkin/issues/167)) ([#169](https://github.com/the-hcma/blumkin/issues/169)) ([411abe6](https://github.com/the-hcma/blumkin/commit/411abe6bc85e9c4cf853968f3b42742c212b392c))
* live_google pytest marker; close Google parity ([#89](https://github.com/the-hcma/blumkin/issues/89)) ([#166](https://github.com/the-hcma/blumkin/issues/166)) ([b01adc5](https://github.com/the-hcma/blumkin/commit/b01adc50a2bf27bc8da95443d2bff787badd6cdf))
* **mail:** mail triage ([#177](https://github.com/the-hcma/blumkin/issues/177)) + auto-reply / vacation responder ([#179](https://github.com/the-hcma/blumkin/issues/179)) ([#188](https://github.com/the-hcma/blumkin/issues/188)) ([5631339](https://github.com/the-hcma/blumkin/commit/563133996c19d25a9b82c29eea475c84970d7ec6))
* **mcp:** blumkin mcp install / status - guided registrar for agent CLIs ([#195](https://github.com/the-hcma/blumkin/issues/195)) ([8265366](https://github.com/the-hcma/blumkin/commit/826536665d3742540c529d7eade3bee3ffea97c0))
* **mcp:** stdio MCP adapter - blumkin mcp serve ([#193](https://github.com/the-hcma/blumkin/issues/193)) ([504de81](https://github.com/the-hcma/blumkin/commit/504de8138e0b646eebdd4a2eb82ad414de173e4c))
* recurring events in calendar create ([#158](https://github.com/the-hcma/blumkin/issues/158)) ([#163](https://github.com/the-hcma/blumkin/issues/163)) ([32dfcf9](https://github.com/the-hcma/blumkin/commit/32dfcf97882ff14f3407feccb336b0ba33256448))


### Bug Fixes

* disable checkout credential persistence in secret-scan and PyPI publish jobs ([#159](https://github.com/the-hcma/blumkin/issues/159)) ([b63e08f](https://github.com/the-hcma/blumkin/commit/b63e08f3adfe1c8a508f1eaac9f3697d18bbafaf))

## [0.2.1](https://github.com/the-hcma/blumkin/compare/blumkin-v0.2.0...blumkin-v0.2.1) (2026-09-03)


### Bug Fixes

* **auth:** typed auth errors, scope-gap detection, and auto re-consent ([#151](https://github.com/the-hcma/blumkin/issues/151)) ([913db19](https://github.com/the-hcma/blumkin/commit/913db19ba5d2fcdfd702c8dbdcdaabd466601a35))


### Documentation

* add PyPI, Python, license, CI, and Release Please badges to the README ([#146](https://github.com/the-hcma/blumkin/issues/146)) ([8ab7ddc](https://github.com/the-hcma/blumkin/commit/8ab7ddc2193ad1f8cfa1d7050aeba7e9a4842765))
* link every CI/static-analysis check to its source; document repository-helpers tooling ([#150](https://github.com/the-hcma/blumkin/issues/150)) ([15e2658](https://github.com/the-hcma/blumkin/commit/15e2658504efd666d457f3ff46dde6e7f95a87af))
* SDLC, security, and review governance; refresh README ([#145](https://github.com/the-hcma/blumkin/issues/145)) ([13f6eff](https://github.com/the-hcma/blumkin/commit/13f6efff0715464aa3d09de4ff33d1ece866315e))

## [0.2.0](https://github.com/the-hcma/blumkin/compare/blumkin-v0.1.0...blumkin-v0.2.0) (2026-09-02)


### Features

* actionable hint on every non-zero blumkin exit ([#101](https://github.com/the-hcma/blumkin/issues/101)) ([a21d4eb](https://github.com/the-hcma/blumkin/commit/a21d4eb4ee058fadc09a8d65834e25c31bf6d09c))
* add mail get for reading a single message ([#56](https://github.com/the-hcma/blumkin/issues/56)) ([d4447a6](https://github.com/the-hcma/blumkin/commit/d4447a6e487b0425a49243ceddc144d009399280))
* add mail reply and mail forward ([#61](https://github.com/the-hcma/blumkin/issues/61)) ([3eb32d7](https://github.com/the-hcma/blumkin/commit/3eb32d77bb5bcbc9bdb725a75c261627ce0c1ce9))
* attach files to mail drafts ([#62](https://github.com/the-hcma/blumkin/issues/62)) ([7112857](https://github.com/the-hcma/blumkin/commit/711285739663a48b1909678c8a518e2733e6efe3))
* automate releases with release-please and PyPI trusted publishing ([#136](https://github.com/the-hcma/blumkin/issues/136)) ([d8b4087](https://github.com/the-hcma/blumkin/commit/d8b40879720fc2298a3e8a107011668569c814c7))
* blumkin completion &lt;bash|zsh|fish&gt; ([#103](https://github.com/the-hcma/blumkin/issues/103)) ([d237248](https://github.com/the-hcma/blumkin/commit/d237248db673ee80d08b0b080387187035193e0e))
* blumkin upgrade over pipx, reporting the build it moved from and to ([#137](https://github.com/the-hcma/blumkin/issues/137)) ([4aa2d97](https://github.com/the-hcma/blumkin/commit/4aa2d97f0b26dc804f906e86aabd645ddf6ea00b))
* calendar suggest mutual free slots ([#78](https://github.com/the-hcma/blumkin/issues/78)) ([aeca763](https://github.com/the-hcma/blumkin/commit/aeca763d074b6a2edd2ba4d3c71995ddc7971b95))
* chat attachment expand fix and Teams-default calendar create ([#81](https://github.com/the-hcma/blumkin/issues/81)) ([f670f95](https://github.com/the-hcma/blumkin/commit/f670f9505d97c33b456b188dcda97343cc2fab41))
* chat last --contains keyword filter ([#122](https://github.com/the-hcma/blumkin/issues/122)) ([275e355](https://github.com/the-hcma/blumkin/commit/275e3558852c6898b1bfcde3b67c7656f90f9898))
* configurable mail signature with --no-signature ([#75](https://github.com/the-hcma/blumkin/issues/75)) ([79a6657](https://github.com/the-hcma/blumkin/commit/79a6657fcee6500cd610c3f97dd3cf6ad6bd11a5))
* embed build metadata and report version, commit, and binary path ([#135](https://github.com/the-hcma/blumkin/issues/135)) ([8cb1284](https://github.com/the-hcma/blumkin/commit/8cb12842d5114f5be553f3371777c1083b3670d5))
* expose build in skills list and document the pipx install and release flow ([#138](https://github.com/the-hcma/blumkin/issues/138)) ([da89215](https://github.com/the-hcma/blumkin/commit/da89215d2818b3d977e79fc4d593c3e08303721c))
* filter and search mail listings ([#57](https://github.com/the-hcma/blumkin/issues/57)) ([9b181d3](https://github.com/the-hcma/blumkin/commit/9b181d328b0f2ba3b2964b4b368977655b866c0f))
* gate Phase 4 MSAL scopes behind WO1162425 flag ([#40](https://github.com/the-hcma/blumkin/issues/40)) ([0592266](https://github.com/the-hcma/blumkin/commit/059226694b5459e9c602974f2521a4bd97a1fa3e))
* Google calendar accept, cancel, and update ([#127](https://github.com/the-hcma/blumkin/issues/127)) ([7739bc6](https://github.com/the-hcma/blumkin/commit/7739bc6a5dd0499d70037ba9ab3a371e46d66c4e))
* Google calendar create and cross-provider email reminder ([#107](https://github.com/the-hcma/blumkin/issues/107)) ([a2cd692](https://github.com/the-hcma/blumkin/commit/a2cd69221d4758993e3fcee4224c9201da68b4d3))
* Google Chat find and last ([#130](https://github.com/the-hcma/blumkin/issues/130)) ([0b9664d](https://github.com/the-hcma/blumkin/commit/0b9664d2c9d3b885bff3540527d12e7d907a37a6))
* Google Chat send, edit, delete, and attachments ([#131](https://github.com/the-hcma/blumkin/issues/131)) ([caa4496](https://github.com/the-hcma/blumkin/commit/caa449606bf839ec23a9a32bf91740b3936200bc))
* Google Gmail draft writes (draft/update-draft/delete-draft/send-draft) ([#108](https://github.com/the-hcma/blumkin/issues/108)) ([78d0b90](https://github.com/the-hcma/blumkin/commit/78d0b90915dfe8d4dc91f157251a130a82f2d9eb))
* Google Gmail reply and forward ([#109](https://github.com/the-hcma/blumkin/issues/109)) ([3b1fdab](https://github.com/the-hcma/blumkin/commit/3b1fdabf0a3d9b2a4255e10617813d0422886b79))
* Google mail attachments and folders ([#111](https://github.com/the-hcma/blumkin/issues/111)) ([a1328e1](https://github.com/the-hcma/blumkin/commit/a1328e108413992101ed76cccdc627f80c3a9b4c))
* Google people resolve ([#129](https://github.com/the-hcma/blumkin/issues/129)) ([860f5c7](https://github.com/the-hcma/blumkin/commit/860f5c7c792cc1a44be0658535a00f3462787339))
* Google Workspace provider read MVP ([#90](https://github.com/the-hcma/blumkin/issues/90)) ([72d6d92](https://github.com/the-hcma/blumkin/commit/72d6d9217fbab695618353f44699f0fbc9446702))
* HTML mail drafts, body-file, and delete-draft ([#33](https://github.com/the-hcma/blumkin/issues/33)) ([8f1232d](https://github.com/the-hcma/blumkin/commit/8f1232d92a27cd18b147e8807d2f365302dddd9f))
* list and download Teams chat message attachments ([#44](https://github.com/the-hcma/blumkin/issues/44)) ([db7874b](https://github.com/the-hcma/blumkin/commit/db7874b85e7517fe007b0afe9dce991605bd7506))
* M1 blumkin CLI skeleton (auth, calendar.today, Cursor skill) ([#10](https://github.com/the-hcma/blumkin/issues/10)) ([2b76313](https://github.com/the-hcma/blumkin/commit/2b763132e12f597d5967cdae0de7e2152ea86bd3))
* mail --importance / --has-attachments server-side filters ([#105](https://github.com/the-hcma/blumkin/issues/105)) ([7996691](https://github.com/the-hcma/blumkin/commit/7996691fde8b808a7d9272a1d33ef85b99a55aaa))
* mail attachments list and download skills ([#43](https://github.com/the-hcma/blumkin/issues/43)) ([138dd8a](https://github.com/the-hcma/blumkin/commit/138dd8a761f48add6d0bc9bee1cf104c9a926610))
* mail reply/forward --cc/--bcc (merge on create) ([#77](https://github.com/the-hcma/blumkin/issues/77)) ([adba565](https://github.com/the-hcma/blumkin/commit/adba56556c6f2af6b5cc95763ac97cfd4251095b))
* mail signature command; update-draft keeps signature and quoted thread ([#125](https://github.com/the-hcma/blumkin/issues/125)) ([0d7ead2](https://github.com/the-hcma/blumkin/commit/0d7ead2fa357a3abc1ffd6c0004af82bfb8dafad))
* mail update-draft PATCH for in-place draft edits ([#34](https://github.com/the-hcma/blumkin/issues/34)) ([abcbc0c](https://github.com/the-hcma/blumkin/commit/abcbc0cc6389f1bf909b38a267ff2dc936e37bdb))
* multi --to/--cc/--bcc on mail draft and update-draft ([#63](https://github.com/the-hcma/blumkin/issues/63)) ([5780e88](https://github.com/the-hcma/blumkin/commit/5780e8863f50b8034fdc0af92ff3274ab22909e8))
* multi-profile config and agent profile protocol ([#92](https://github.com/the-hcma/blumkin/issues/92)) ([d95f64c](https://github.com/the-hcma/blumkin/commit/d95f64c894743f57895d62b71711a50000c33780))
* people resolve (fail-closed on ambiguous matches) ([#79](https://github.com/the-hcma/blumkin/issues/79)) ([c6d525a](https://github.com/the-hcma/blumkin/commit/c6d525a41531df5fd2d3fec7c412ddcda49f0502))
* Phase 2 read skills (calendar, mail, chat) ([#25](https://github.com/the-hcma/blumkin/issues/25)) ([9f085ce](https://github.com/the-hcma/blumkin/commit/9f085ceac1e8e28be91b8a0bc995a52f91e8e63b))
* Phase 3 write skills with --yes gating ([#28](https://github.com/the-hcma/blumkin/issues/28)) ([f1c87d8](https://github.com/the-hcma/blumkin/commit/f1c87d8a555abac2f4e6274ad126aa178314456d))
* Phase 4 chat write and meeting transcription ([#37](https://github.com/the-hcma/blumkin/issues/37)) ([ff97989](https://github.com/the-hcma/blumkin/commit/ff9798902f748763b757c89341bbf1b36b3959d4))
* read mail from any folder, not just the default collection ([#49](https://github.com/the-hcma/blumkin/issues/49)) ([713419b](https://github.com/the-hcma/blumkin/commit/713419b8fe86f8a36b8d19ed2af1ab6343dd526f))
* record the account email per profile ([#124](https://github.com/the-hcma/blumkin/issues/124)) ([76ab558](https://github.com/the-hcma/blumkin/commit/76ab5589e808cbcf6ecd75f1299741858532d1a0))
* surface freebusy working hours and attendee timezone ([#70](https://github.com/the-hcma/blumkin/issues/70)) ([3073f62](https://github.com/the-hcma/blumkin/commit/3073f62f91600ddb1f5f5eeb9c7913110edc7dae))
* top-level `ok` boolean on every --json stdout payload ([#104](https://github.com/the-hcma/blumkin/issues/104)) ([328c617](https://github.com/the-hcma/blumkin/commit/328c617654123260291ee49cc5f9dbef15494894))
* WorkspaceProvider abstraction with Microsoft adapter ([#85](https://github.com/the-hcma/blumkin/issues/85)) ([6f77f4e](https://github.com/the-hcma/blumkin/commit/6f77f4efcb188d8067d1725112065f2c842e1315))


### Bug Fixes

* backfill the profile email for already-authenticated profiles ([#126](https://github.com/the-hcma/blumkin/issues/126)) ([67dc76a](https://github.com/the-hcma/blumkin/commit/67dc76a56c3c29f76e7135f39c75cd15aea5643a))
* chat last --chat-id, and fail closed on an ambiguous --with ([#121](https://github.com/the-hcma/blumkin/issues/121)) ([79d76fb](https://github.com/the-hcma/blumkin/commit/79d76fbf1cb35df13dfef20bc7c3404f176709bf))
* freebusy working_hours TimeOfDay strings and timezone None ([#71](https://github.com/the-hcma/blumkin/issues/71)) ([4898006](https://github.com/the-hcma/blumkin/commit/4898006802827a97bc9aade4c6df8872ca11474d))
* Gmail update-draft preserves message structure; address [#108](https://github.com/the-hcma/blumkin/issues/108) review ([#112](https://github.com/the-hcma/blumkin/issues/112)) ([d44f223](https://github.com/the-hcma/blumkin/commit/d44f223a50ea0acd804006be0ea4f4079b5b6fca))
* Graph HTTP timeouts and noninteractive auth refresh ([#82](https://github.com/the-hcma/blumkin/issues/82)) ([4cbd712](https://github.com/the-hcma/blumkin/commit/4cbd712dfce59667838a98338758050ccd426400))
* honor DateTimeTimeZone.timeZone when rendering Graph event times ([#48](https://github.com/the-hcma/blumkin/issues/48)) ([8d48565](https://github.com/the-hcma/blumkin/commit/8d48565d97b3ad669fc163427e7ff677581c2b4f))
* skip per-chat 403 in chat_find member fetch ([#27](https://github.com/the-hcma/blumkin/issues/27)) ([b795ec3](https://github.com/the-hcma/blumkin/commit/b795ec3eb5b2971f8725e33f46ad3e314e2c6f24))
* tighten MSAL cache and auth-record file modes on rewrite ([#72](https://github.com/the-hcma/blumkin/issues/72)) ([19e0e29](https://github.com/the-hcma/blumkin/commit/19e0e29edd0d904c3e1487f12fdf00e9f7f3e215))
* use Calendars.ReadWrite for silent MSAL refresh ([#24](https://github.com/the-hcma/blumkin/issues/24)) ([aec0363](https://github.com/the-hcma/blumkin/commit/aec03632c2ed2ad538a72939b4561987ccb6570b))


### Documentation

* authoring style — hyphens, not em dashes in message bodies ([#64](https://github.com/the-hcma/blumkin/issues/64)) ([699769f](https://github.com/the-hcma/blumkin/commit/699769f8a60c57f87cbba4612ee8a90a472d160f))
* close M1 retrospective ([#11](https://github.com/the-hcma/blumkin/issues/11)) ([#36](https://github.com/the-hcma/blumkin/issues/36)) ([12e6cce](https://github.com/the-hcma/blumkin/commit/12e6cce60c3c6bdccbd72e485ca016baba76034d))
* comprehensive CLI help with usage examples ([#96](https://github.com/the-hcma/blumkin/issues/96)) ([ea86f0f](https://github.com/the-hcma/blumkin/commit/ea86f0f07cce8c3785d8babe5bb75f8d2a200ad4))
* folder count lag + Outlook-safe HTML guidance ([#69](https://github.com/the-hcma/blumkin/issues/69)) ([7177adb](https://github.com/the-hcma/blumkin/commit/7177adb0a38bdddff9bf1eafcb68172fda7e2d58))
* forbid verifying blumkin with skills that notify other people ([#51](https://github.com/the-hcma/blumkin/issues/51)) ([8796c7c](https://github.com/the-hcma/blumkin/commit/8796c7c0775a5171200b2abaac9387e4d4c6e0df))
* Google Desktop OAuth secret is shown once ([#120](https://github.com/the-hcma/blumkin/issues/120)) ([be39520](https://github.com/the-hcma/blumkin/commit/be3952016f3d897e3d09617942c831b5793edcac))
* keep Remedy WO details in private lab only ([#8](https://github.com/the-hcma/blumkin/issues/8)) ([76caf0e](https://github.com/the-hcma/blumkin/commit/76caf0e6d5956ae772b9ed368d024ff89d5d7051))
* M1 closeout (PATH install, plan status, CVE check) ([#23](https://github.com/the-hcma/blumkin/issues/23)) ([a22560b](https://github.com/the-hcma/blumkin/commit/a22560bf86421c95bc427cd68c1420ccd1ad6fd7))
* publish agent integration guide and freeze the skills JSON contract ([#52](https://github.com/the-hcma/blumkin/issues/52)) ([b31233b](https://github.com/the-hcma/blumkin/commit/b31233b7b969aa8d0ada0012a765360b87f38aad))
* record Identity follow-up WO0000001162425 ([#7](https://github.com/the-hcma/blumkin/issues/7)) ([494634e](https://github.com/the-hcma/blumkin/commit/494634e5f4333f611bda189b6b93ba412d8a94b9))
* scrub personal paths; invoke blumkin on PATH ([#6](https://github.com/the-hcma/blumkin/issues/6)) ([66b4133](https://github.com/the-hcma/blumkin/commit/66b41335033de4cfed8aaed7288298fd1f2a67b2))
* sync agent skill and integration guide after recent landings ([#74](https://github.com/the-hcma/blumkin/issues/74)) ([5491326](https://github.com/the-hcma/blumkin/commit/5491326b22faaecc46c92161b65e0f8f37566dc3))

## Changelog

All notable changes are recorded here by
[Release Please](https://github.com/googleapis/release-please) from Conventional
Commit messages. Do not edit this file by hand.
