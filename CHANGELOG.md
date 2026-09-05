# Changelog

## [0.2.2](https://github.com/the-hcma/blumkin/compare/blumkin-v0.2.1...blumkin-v0.2.2) (2026-09-05)


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
