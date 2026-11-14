# Changelog

## [1.14.1](https://github.com/the-hcma/blumkin/compare/blumkin-v1.14.0...blumkin-v1.14.1) (2026-11-14)


### Documentation

* bring the guides, skills and release docs in line with the code before the 1.14.0 publish ([#752](https://github.com/the-hcma/blumkin/issues/752)) ([e711b50](https://github.com/the-hcma/blumkin/commit/e711b505252a9648258b6865515bf6adc97218b5))

## [1.14.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.13.0...blumkin-v1.14.0) (2026-11-14)


### Features

* **browser:** --nth picks one of several identically named controls ([#573](https://github.com/the-hcma/blumkin/issues/573)) ([#579](https://github.com/the-hcma/blumkin/issues/579)) ([871521e](https://github.com/the-hcma/blumkin/commit/871521ee09f711e5bfcd9b6dd72035f735819974))
* **browser:** a cell command for Oracle JET data grids, and no restore-pages bubble on launch ([#652](https://github.com/the-hcma/blumkin/issues/652)) ([8c084cc](https://github.com/the-hcma/blumkin/commit/8c084cc1efd7825a9aa419763867a8f59fbdf7f7))
* **browser:** a flow pick step can choose a gridcell and match the s… ([#686](https://github.com/the-hcma/blumkin/issues/686)) ([cfb0fb1](https://github.com/the-hcma/blumkin/commit/cfb0fb1704138db1f853058e2d726ab3f0fc4d11))
* **browser:** a flow press step sends a key and a fill step can type a literal value ([#702](https://github.com/the-hcma/blumkin/issues/702)) ([2055be2](https://github.com/the-hcma/blumkin/commit/2055be2dbc0146ef444c0483571d56e53a9eb81b))
* **browser:** a flow select step chooses a dropdown option from a bi… ([#687](https://github.com/the-hcma/blumkin/issues/687)) ([e6e3f58](https://github.com/the-hcma/blumkin/commit/e6e3f583e4e2eead1e89b528f39a85aba19c41f7))
* **browser:** a flow step can say which of several same-named controls it means with nth ([#696](https://github.com/the-hcma/blumkin/issues/696)) ([#698](https://github.com/the-hcma/blumkin/issues/698)) ([af9d575](https://github.com/the-hcma/blumkin/commit/af9d575b73af8dad3c94ea74b0fa20dbefcb642c))
* **browser:** a heading miss names the dialog that opened over the p… ([#694](https://github.com/the-hcma/blumkin/issues/694)) ([15c5f05](https://github.com/the-hcma/blumkin/commit/15c5f05cefc3b2aab49a183f798cfae41cebc8e8))
* **browser:** a read-only inspect for an element's classes and state… ([#712](https://github.com/the-hcma/blumkin/issues/712)) ([f88ebc1](https://github.com/the-hcma/blumkin/commit/f88ebc167c92033c1efb3fd8adaa1554a505a912))
* **browser:** a validated models_dir setting puts the site models in a folder of your choice ([#621](https://github.com/the-hcma/blumkin/issues/621)) ([#643](https://github.com/the-hcma/blumkin/issues/643)) ([28a30d4](https://github.com/the-hcma/blumkin/commit/28a30d4affb0691b9e4e3104bc0859eb5c7d607a))
* **browser:** an upload step on a button-recorded control matches it… ([#717](https://github.com/the-hcma/blumkin/issues/717)) ([bec1ada](https://github.com/the-hcma/blumkin/commit/bec1ada1a68e3460996de57638919468ed434207))
* **browser:** browser click --record adds the click as a step of the draft ([#608](https://github.com/the-hcma/blumkin/issues/608), [#624](https://github.com/the-hcma/blumkin/issues/624)) ([#671](https://github.com/the-hcma/blumkin/issues/671)) ([f9ba855](https://github.com/the-hcma/blumkin/commit/f9ba8554ad7b350c6e48f0444615430b8f7b8d20))
* **browser:** browser close stops a task's browser and keeps its profile ([#574](https://github.com/the-hcma/blumkin/issues/574)) ([#580](https://github.com/the-hcma/blumkin/issues/580)) ([d52f43d](https://github.com/the-hcma/blumkin/commit/d52f43d5115304091721a66921183fb95aaf2c33))
* **browser:** browser commands and [browser] default config ([#482](https://github.com/the-hcma/blumkin/issues/482)) ([efdd19a](https://github.com/the-hcma/blumkin/commit/efdd19aff265bf6588e3fb002b30caa27c06a156))
* **browser:** browser dialog opens a dialog or menu through declared controls, reads it and always closes it ([#598](https://github.com/the-hcma/blumkin/issues/598)) ([#648](https://github.com/the-hcma/blumkin/issues/648)) ([123487b](https://github.com/the-hcma/blumkin/commit/123487bdc0d84e2257c15c09e19d14dc341a003f))
* **browser:** browser pick chooses a result row or cell by its text, never one that is or holds a control ([#608](https://github.com/the-hcma/blumkin/issues/608)) ([#651](https://github.com/the-hcma/blumkin/issues/651)) ([6d8d2ee](https://github.com/the-hcma/blumkin/commit/6d8d2ee54af44115743fdcff42ca4b9e40ab37e7))
* **browser:** browser raise brings a task's window to the front, and launch and a sign-in goto do too ([#656](https://github.com/the-hcma/blumkin/issues/656)) ([#666](https://github.com/the-hcma/blumkin/issues/666)) ([53c3940](https://github.com/the-hcma/blumkin/commit/53c3940ab00fed91f5fb23d304f71feb6f53b0d4))
* **browser:** capture a Teams transcript pane end to end with a gap report ([#518](https://github.com/the-hcma/blumkin/issues/518)) ([da51867](https://github.com/the-hcma/blumkin/commit/da518679a1bcc0de327cfd479a7e2f4a51aa26cb))
* **browser:** CDP attach layer on Playwright with loopback-only, bounded retries and detach-only close ([#475](https://github.com/the-hcma/blumkin/issues/475)) ([0aa5ab9](https://github.com/the-hcma/blumkin/commit/0aa5ab9c732911fc2595f89dcb3b410b64c12ad8))
* **browser:** cell is a flow step, text: names a digits-only grid header and ledger expect changes an instance's counts ([#657](https://github.com/the-hcma/blumkin/issues/657)) ([#667](https://github.com/the-hcma/blumkin/issues/667)) ([62c069b](https://github.com/the-hcma/blumkin/commit/62c069b39182054d1d7fbb601b3ce4f2e1d657b8))
* **browser:** check a recorded site model against the live site ([#487](https://github.com/the-hcma/blumkin/issues/487)) ([#500](https://github.com/the-hcma/blumkin/issues/500)) ([10597bd](https://github.com/the-hcma/blumkin/commit/10597bdc14db07b95f0bff2318b8af32ab57e969))
* **browser:** click waits for the page to settle, reports where it ended and --expect-heading fails clearly ([#625](https://github.com/the-hcma/blumkin/issues/625)) ([#659](https://github.com/the-hcma/blumkin/issues/659)) ([f570537](https://github.com/the-hcma/blumkin/commit/f57053762f26c69a0f66621d90c42506aa772f94))
* **browser:** code-enforced submit guard around the CDP driver ([#476](https://github.com/the-hcma/blumkin/issues/476)) ([9d4c183](https://github.com/the-hcma/blumkin/commit/9d4c1830c9ff6b3dc178c369e8f5d4a4ed46aba9))
* **browser:** control names in allowed_controls can hold a * for any text ([#588](https://github.com/the-hcma/blumkin/issues/588)) ([#604](https://github.com/the-hcma/blumkin/issues/604)) ([bfc58b4](https://github.com/the-hcma/blumkin/commit/bfc58b4195e0935456b06419c5f536b0c8ab4f85))
* **browser:** dialog_confirm lets a task click a dialog's own confirm button, only inside a dialog ([#587](https://github.com/the-hcma/blumkin/issues/587)) ([#603](https://github.com/the-hcma/blumkin/issues/603)) ([3a13b2e](https://github.com/the-hcma/blumkin/commit/3a13b2ec3b08cde35ebafceb4ca7088c5015bc5d))
* **browser:** dialog_landmarks lets a declared dialog confirm work in a popup the page does not mark as a dialog ([#624](https://github.com/the-hcma/blumkin/issues/624)) ([#658](https://github.com/the-hcma/blumkin/issues/658)) ([3aa9ff2](https://github.com/the-hcma/blumkin/commit/3aa9ff2d306c4d07c2c780fe81613165186bf28b))
* **browser:** download a text file the page offers into a declared folder ([#546](https://github.com/the-hcma/blumkin/issues/546)) ([#556](https://github.com/the-hcma/blumkin/issues/556)) ([9f1f375](https://github.com/the-hcma/blumkin/commit/9f1f375017246e82cb5a9095d4e0bd7c8f45fc3c))
* **browser:** draft_controls lets a user-verified recalculation button through the submit check ([#614](https://github.com/the-hcma/blumkin/issues/614)) ([#633](https://github.com/the-hcma/blumkin/issues/633)) ([e8db4e8](https://github.com/the-hcma/blumkin/commit/e8db4e8b5bfbedf778a017c532349b46733b3476))
* **browser:** each task window shows its own colour and a blumkin pill while attached ([#590](https://github.com/the-hcma/blumkin/issues/590)) ([#610](https://github.com/the-hcma/blumkin/issues/610)) ([fc2156d](https://github.com/the-hcma/blumkin/commit/fc2156dc6657e4ec8429044350ead31360481492))
* **browser:** expense-report and time-sheet skills ([#493](https://github.com/the-hcma/blumkin/issues/493)) ([46a506d](https://github.com/the-hcma/blumkin/commit/46a506ddbc7c08ebed0af37f2328a96ea69d62f3))
* **browser:** explore a site once and record its site model ([#498](https://github.com/the-hcma/blumkin/issues/498)) ([413dc7e](https://github.com/the-hcma/blumkin/commit/413dc7e9a488cd690e2ae9c30bf8e9c3243ae1c1))
* **browser:** explore click runs for controls the task declared in allowed_controls ([#597](https://github.com/the-hcma/blumkin/issues/597)) ([#647](https://github.com/the-hcma/blumkin/issues/647)) ([f5ed4bd](https://github.com/the-hcma/blumkin/commit/f5ed4bd248ecd15128c00e671cc35e312f6ae59c))
* **browser:** explore records a popup as a dialog with its landmarks and a list-of-values page, and flows can pick a result row ([#624](https://github.com/the-hcma/blumkin/issues/624), [#608](https://github.com/the-hcma/blumkin/issues/608)) ([#669](https://github.com/the-hcma/blumkin/issues/669)) ([4087584](https://github.com/the-hcma/blumkin/commit/40875848ee8fbe3753c3af2a0b0419f1b611826c))
* **browser:** explore stops flagging fields as finalizing, can forget a page and warns about shared addresses ([#608](https://github.com/the-hcma/blumkin/issues/608)) ([#630](https://github.com/the-hcma/blumkin/issues/630)) ([a700e49](https://github.com/the-hcma/blumkin/commit/a700e494c6ca7e295798f11eb9ed037e9ddf0cf9))
* **browser:** fill and verify take a sensitive value privately and never print the field's value ([#617](https://github.com/the-hcma/blumkin/issues/617)) ([#641](https://github.com/the-hcma/blumkin/issues/641)) ([be87a84](https://github.com/the-hcma/blumkin/commit/be87a84968e4f57fb70790dc0fd39c910bc61a3b))
* **browser:** fill, select and upload wait for the page to settle like click, with --no-settle ([#625](https://github.com/the-hcma/blumkin/issues/625)) ([#662](https://github.com/the-hcma/blumkin/issues/662)) ([9960193](https://github.com/the-hcma/blumkin/commit/996019391e40b9eca04bd8c63e28b0839954d4a2))
* **browser:** fill, upload and select can target repeated labels, dropdowns and frames ([#586](https://github.com/the-hcma/blumkin/issues/586)) ([#602](https://github.com/the-hcma/blumkin/issues/602)) ([e17aae2](https://github.com/the-hcma/blumkin/commit/e17aae2727b29c1fc0354d2e1ec8d58799783df3))
* **browser:** flow add refuses an upload step on a button-recorded control without nth ([#711](https://github.com/the-hcma/blumkin/issues/711)) ([988748b](https://github.com/the-hcma/blumkin/commit/988748bdc1ba2320be216870dc1278bff090a6e3))
* **browser:** flow steps can state a heading to wait for and hand a step to the human, with ledger apply --from-step ([#622](https://github.com/the-hcma/blumkin/issues/622)) ([#664](https://github.com/the-hcma/blumkin/issues/664)) ([bbe4026](https://github.com/the-hcma/blumkin/commit/bbe40267a111b8a40d799a8ed3ef973713c29236))
* **browser:** flow steps can state starts_on, and a verify step checks a field ([#675](https://github.com/the-hcma/blumkin/issues/675)) ([876ae46](https://github.com/the-hcma/blumkin/commit/876ae46e192427133cc10bf343fa7281e1509632))
* **browser:** flows declare typed parameters that ledger apply checks before staging or touching the browser ([#622](https://github.com/the-hcma/blumkin/issues/622)) ([#653](https://github.com/the-hcma/blumkin/issues/653)) ([3a647b3](https://github.com/the-hcma/blumkin/commit/3a647b38c2e723c8c094582f57be99af338a9fe1))
* **browser:** goto reports its redirect chain and reset-session clears a stuck sign-in keeping the profile ([#623](https://github.com/the-hcma/blumkin/issues/623)) ([#663](https://github.com/the-hcma/blumkin/issues/663)) ([fb2514c](https://github.com/the-hcma/blumkin/commit/fb2514cf3fea935b8a856876a13693e178b91d52))
* **browser:** goto waits through the sign-in redirect, form_url and launch --open, browser endpoint ([#589](https://github.com/the-hcma/blumkin/issues/589)) ([#605](https://github.com/the-hcma/blumkin/issues/605)) ([3c6c787](https://github.com/the-hcma/blumkin/commit/3c6c7879f106de9e87382e984aa42bbb0a6d0c76))
* **browser:** guarded action commands and per-task policy ([#492](https://github.com/the-hcma/blumkin/issues/492)) ([712edda](https://github.com/the-hcma/blumkin/commit/712eddaf8ddf27b1c37b9e23d5e98b90dce24093))
* **browser:** handoff contract that can only report submitted false ([#479](https://github.com/the-hcma/blumkin/issues/479)) ([1e2cc16](https://github.com/the-hcma/blumkin/commit/1e2cc1688264816993e3d687af0912e7e6f3b652))
* **browser:** init-task creates a task's profile and starting policy from a template ([#545](https://github.com/the-hcma/blumkin/issues/545)) ([#554](https://github.com/the-hcma/blumkin/issues/554)) ([e26f29f](https://github.com/the-hcma/blumkin/commit/e26f29fd2f3c47891ced0e99928450003f345139))
* **browser:** init-task expense-report writes the whole policy table with host, form_url and upload folder ([#591](https://github.com/the-hcma/blumkin/issues/591)) ([#611](https://github.com/the-hcma/blumkin/issues/611)) ([333ff65](https://github.com/the-hcma/blumkin/commit/333ff6584a21181485a83417f999302ef42a8773))
* **browser:** init-task time-sheet template, Sunday-start periods and the period, ledger and codes rules ([#599](https://github.com/the-hcma/blumkin/issues/599)) ([#650](https://github.com/the-hcma/blumkin/issues/650)) ([e2131c3](https://github.com/the-hcma/blumkin/commit/e2131c37ee6eabff57d12ad10f145694fa62a2c1))
* **browser:** init-task time-sheet writes the sign-in keys, and goto waits for the model page's landmarks ([#705](https://github.com/the-hcma/blumkin/issues/705)) ([75d4c84](https://github.com/the-hcma/blumkin/commit/75d4c840581d8f24332f60820031f25a12bac5d1))
* **browser:** inspect --requests --watch lists the requests a page m… ([#714](https://github.com/the-hcma/blumkin/issues/714)) ([b2ec714](https://github.com/the-hcma/blumkin/commit/b2ec71404a2610c840e52a5cbf224ab01f3afd67))
* **browser:** ledger handoff keeps its decisions in the ledger, and … ([#713](https://github.com/the-hcma/blumkin/issues/713)) ([87a992b](https://github.com/the-hcma/blumkin/commit/87a992b381d40c355edb289da046028aa35a7c4c))
* **browser:** ledger-backed stage, apply, verify, reconcile and hand… ([#514](https://github.com/the-hcma/blumkin/issues/514)) ([87b1f35](https://github.com/the-hcma/blumkin/commit/87b1f35336f03e6905238fbcf689e0a98b76c197))
* **browser:** list and read child frames and take an ARIA snapshot ([#541](https://github.com/the-hcma/blumkin/issues/541)) ([#548](https://github.com/the-hcma/blumkin/issues/548)) ([2cd7c82](https://github.com/the-hcma/blumkin/commit/2cd7c8252f12c3137a4f7be0e6cebede0367003a))
* **browser:** model flow add writes a flow into the site model from short specs ([#622](https://github.com/the-hcma/blumkin/issues/622)) ([#668](https://github.com/the-hcma/blumkin/issues/668)) ([829ffd8](https://github.com/the-hcma/blumkin/commit/829ffd859365dfeabd6817ec795a9293380ee800))
* **browser:** opt-in deny-list mode opens up navigation controls without declaring each one ([#649](https://github.com/the-hcma/blumkin/issues/649)) ([7338d8f](https://github.com/the-hcma/blumkin/commit/7338d8f2ed91db4403a891d9584c1b4725740703))
* **browser:** opt-in strict mode that blocks background writes ([#478](https://github.com/the-hcma/blumkin/issues/478)) ([#561](https://github.com/the-hcma/blumkin/issues/561)) ([b1c0724](https://github.com/the-hcma/blumkin/commit/b1c07241542e07d15f7d573f16c9c4724bb0ac4f))
* **browser:** per-task profiles, browser discovery and launch on loopback DevTools ([#473](https://github.com/the-hcma/blumkin/issues/473)) ([26b6177](https://github.com/the-hcma/blumkin/commit/26b61772785cb2ce3fe970e7f712f2acdbc0a084))
* **browser:** press narrowing, a read-only options list and a grid view for editable grids ([#596](https://github.com/the-hcma/blumkin/issues/596)) ([#646](https://github.com/the-hcma/blumkin/issues/646)) ([b95e4b2](https://github.com/the-hcma/blumkin/commit/b95e4b208daccb7d0121913112ea46b493d40dfa))
* **browser:** read --aria scopes to a subtree, summarizes option lists and pages with offset and limit ([#606](https://github.com/the-hcma/blumkin/issues/606)) ([#628](https://github.com/the-hcma/blumkin/issues/628)) ([c11ec95](https://github.com/the-hcma/blumkin/commit/c11ec95115cd525ffafb928e8b29b8f74644cdbf))
* **browser:** read --compact, model flows, and a skill rule against … ([#706](https://github.com/the-hcma/blumkin/issues/706)) ([a922f9b](https://github.com/the-hcma/blumkin/commit/a922f9bdc2777b7d885f5709f115c30461a4c8b8))
* **browser:** read shows a recognized sign-in page outside allowed hosts and status --verbose shows where the browser is ([#623](https://github.com/the-hcma/blumkin/issues/623)) ([#654](https://github.com/the-hcma/blumkin/issues/654)) ([ca24a25](https://github.com/the-hcma/blumkin/commit/ca24a25a1510c7c51976669748871c299bd35699))
* **browser:** rehome confirmed receipts under a canonical name ([#491](https://github.com/the-hcma/blumkin/issues/491)) ([#516](https://github.com/the-hcma/blumkin/issues/516)) ([08a6ef8](https://github.com/the-hcma/blumkin/commit/08a6ef87bc07a98a3dc44f162fbef8303638394a))
* **browser:** sign-in picks the profile's own account on an identity provider's picker ([#616](https://github.com/the-hcma/blumkin/issues/616)) ([#636](https://github.com/the-hcma/blumkin/issues/636)) ([e2c22c1](https://github.com/the-hcma/blumkin/commit/e2c22c1e9d151f20333dd1dc94f5e2e5e04f9425))
* **browser:** task instance ledger for incremental time sheets and e… ([#513](https://github.com/the-hcma/blumkin/issues/513)) ([22247c8](https://github.com/the-hcma/blumkin/commit/22247c8915fb9c4d7cacde6ea56b47c345d43c5c))
* **browser:** the expense-report template enables the in-form dialog… ([#691](https://github.com/the-hcma/blumkin/issues/691)) ([6ee2bd5](https://github.com/the-hcma/blumkin/commit/6ee2bd58c10efd1b0564f7a5332876f0fadc575d))
* **browser:** the ledger can record a receipt as deliberately excluded, and take it back ([#709](https://github.com/the-hcma/blumkin/issues/709)) ([e4d6e51](https://github.com/the-hcma/blumkin/commit/e4d6e519c2a66146380be1cf23f59f7f8baeebd1))
* **browser:** the ledger records the report and how a session ended, and says what is on the server ([#592](https://github.com/the-hcma/blumkin/issues/592)) ([#619](https://github.com/the-hcma/blumkin/issues/619)) ([0218514](https://github.com/the-hcma/blumkin/commit/0218514f7e1faa93d6289a608e9fb3e1a5b6d763))
* **browser:** the ledger remembers where a flow stopped, so a plain re-run does not repeat its steps ([#622](https://github.com/the-hcma/blumkin/issues/622)) ([#670](https://github.com/the-hcma/blumkin/issues/670)) ([fb8f793](https://github.com/the-hcma/blumkin/commit/fb8f793d90789e94e8fe8dd7a96daa9e07af42e1))
* **browser:** upload reports what the page shows of the file, and a label shared with hidden fields means the visible one ([#607](https://github.com/the-hcma/blumkin/issues/607)) ([#629](https://github.com/the-hcma/blumkin/issues/629)) ([817f361](https://github.com/the-hcma/blumkin/commit/817f36171b0fae8fc9b52ae5d923cc7bcc7a468d))
* **browser:** versioned on-disk site model store ([#496](https://github.com/the-hcma/blumkin/issues/496)) ([ffa39c4](https://github.com/the-hcma/blumkin/commit/ffa39c45a44fc761f76cc3282df6acd12ff9decd))
* **browser:** wait for late content, role-less click and refresh-tab ([#542](https://github.com/the-hcma/blumkin/issues/542)) ([#549](https://github.com/the-hcma/blumkin/issues/549)) ([63bac14](https://github.com/the-hcma/blumkin/commit/63bac1439b6944a71fcd7c7a6d111df21517786d))
* **calendar:** a read-only calendar check an agent runs before proposing a time in a mail or chat message ([#723](https://github.com/the-hcma/blumkin/issues/723)) ([d8d7f63](https://github.com/the-hcma/blumkin/commit/d8d7f63158012888f9ba8b5b48d5bf5f0fa88a5b))
* **calendar:** refuse a booking on a weekend, a holiday, off-hours o… ([#719](https://github.com/the-hcma/blumkin/issues/719)) ([b736b52](https://github.com/the-hcma/blumkin/commit/b736b520a076bfc8f6fbd33f4246763621fdced0))
* **calendar:** tell your own out-of-office entry from a colleague's accepted OOO event ([#730](https://github.com/the-hcma/blumkin/issues/730)) ([541a70e](https://github.com/the-hcma/blumkin/commit/541a70ed83bb5873c2ad8a79bcc5bd55f44183a3))
* **ci:** lay out temp files as TMPDIR/&lt;project&gt;/&lt;branch&gt;/&lt;tool&gt; ([#699](https://github.com/the-hcma/blumkin/issues/699)) ([7015468](https://github.com/the-hcma/blumkin/commit/701546858079147833793d7846400956e6979d82))
* **dev:** run the checkout with scripts/dev/blumkin, dev version marker and guided first-run setup ([#524](https://github.com/the-hcma/blumkin/issues/524)) ([#532](https://github.com/the-hcma/blumkin/issues/532)) ([05e0ce3](https://github.com/the-hcma/blumkin/commit/05e0ce314be08bf0904fded17b2ffe7841a7bbba))
* **doctor:** flag an install that lacks commands a nearby checkout has ([#547](https://github.com/the-hcma/blumkin/issues/547)) ([#558](https://github.com/the-hcma/blumkin/issues/558)) ([c538b22](https://github.com/the-hcma/blumkin/commit/c538b222dd3d741bfb288b045a98c30d028c143c))
* **drive:** upload a local file to Google Drive or OneDrive ([#555](https://github.com/the-hcma/blumkin/issues/555)) ([#559](https://github.com/the-hcma/blumkin/issues/559)) ([bfb382a](https://github.com/the-hcma/blumkin/commit/bfb382ad01158db47fdf7ef4622243388c8185ef))
* **loop:** blumkin loop agent runs your agent in a pty and logs the session, plus value completion ([#538](https://github.com/the-hcma/blumkin/issues/538)) ([#540](https://github.com/the-hcma/blumkin/issues/540)) ([3489cff](https://github.com/the-hcma/blumkin/commit/3489cffc18c9f8a6395ff0dd3dbebb6382952f3a))
* **loop:** cron due-time catch-up and end-of-day time-sheet trigger ([#502](https://github.com/the-hcma/blumkin/issues/502)) ([#509](https://github.com/the-hcma/blumkin/issues/509)) ([84ab5b5](https://github.com/the-hcma/blumkin/commit/84ab5b5ec8f4a0218aaf288faf03cb6278324994))
* **loop:** mail trigger init, poll, allowlist and attachment intake ([#503](https://github.com/the-hcma/blumkin/issues/503)) ([#510](https://github.com/the-hcma/blumkin/issues/510)) ([3d9a5a5](https://github.com/the-hcma/blumkin/commit/3d9a5a540806df7f3087b1aced147f8e8502c07f))
* **loop:** poll mail incrementally with Graph delta and Gmail history ([#525](https://github.com/the-hcma/blumkin/issues/525)) ([#531](https://github.com/the-hcma/blumkin/issues/531)) ([38d00d0](https://github.com/the-hcma/blumkin/commit/38d00d07598a10da440a643f82419d9e527d2e54))
* **loop:** queue lesson page photos for the operator's agent and reply with the Drive link ([#506](https://github.com/the-hcma/blumkin/issues/506)) ([#526](https://github.com/the-hcma/blumkin/issues/526)) ([bd348ea](https://github.com/the-hcma/blumkin/commit/bd348eac173a9d4ddd0450b1ba05fc4d1b1ce5ef))
* **loop:** reply-channel notices for runs and profile pauses ([#505](https://github.com/the-hcma/blumkin/issues/505)) ([#511](https://github.com/the-hcma/blumkin/issues/511)) ([ee1a970](https://github.com/the-hcma/blumkin/commit/ee1a970e58d6ab066f39967e390bb1769cc481ed))
* **loop:** resolve instances and file expense receipts from mail ([#504](https://github.com/the-hcma/blumkin/issues/504)) ([#520](https://github.com/the-hcma/blumkin/issues/520)) ([645aef5](https://github.com/the-hcma/blumkin/commit/645aef5ceb2895a34d0f1553f5d81bb6e4ba9de9))
* **loop:** runner core with work queue, journal, kill switch and service ([#501](https://github.com/the-hcma/blumkin/issues/501)) ([#508](https://github.com/the-hcma/blumkin/issues/508)) ([9abe19e](https://github.com/the-hcma/blumkin/commit/9abe19ea3592085eb0b70077ea8afa743fd5a47f))
* **profiles:** an optional physical address on a profile, readable only by an authorized browser task ([#613](https://github.com/the-hcma/blumkin/issues/613)) ([#632](https://github.com/the-hcma/blumkin/issues/632)) ([017c48c](https://github.com/the-hcma/blumkin/commit/017c48c7ebb457bab4b93b870317996b855c27dc))
* **receipts:** better vendor names, quiet PDF logs, photo OCR prep, find --new-since and to-pdf ([#585](https://github.com/the-hcma/blumkin/issues/585)) ([#601](https://github.com/the-hcma/blumkin/issues/601)) ([cbe934e](https://github.com/the-hcma/blumkin/commit/cbe934ed9a343ef709d4f1763e1e28bc462fefe7))
* **receipts:** find receipt files and read vendor, date, total and currency with confidence ([#483](https://github.com/the-hcma/blumkin/issues/483)) ([c55bd7d](https://github.com/the-hcma/blumkin/commit/c55bd7dce6cf470b2c0c6ed92ac1ce6f017b22fc))
* **receipts:** keep amounts and archive paths out of command logs with private input, opaque references and redacted results ([#618](https://github.com/the-hcma/blumkin/issues/618)) ([#642](https://github.com/the-hcma/blumkin/issues/642)) ([967df0c](https://github.com/the-hcma/blumkin/commit/967df0ca3e164d80827bc840d3ab7e8105b1a293))
* **receipts:** mail export renders a message to a verified PDF with text and JSON files ([#583](https://github.com/the-hcma/blumkin/issues/583)) ([#594](https://github.com/the-hcma/blumkin/issues/594)) ([f7ab04c](https://github.com/the-hcma/blumkin/commit/f7ab04ceeecc7c8460f5961755c70ce33013232f))
* **receipts:** photos are straightened, downscaled and retried with other layouts before OCR gives up, and an empty read says so ([#627](https://github.com/the-hcma/blumkin/issues/627)) ([#661](https://github.com/the-hcma/blumkin/issues/661)) ([2a8028d](https://github.com/the-hcma/blumkin/commit/2a8028d4d3fe78c510b181b43727cfb18448fb93))
* **receipts:** summarize trips, drop without-tip copies, confirm before rehome and replace a bad render ([#584](https://github.com/the-hcma/blumkin/issues/584)) ([#600](https://github.com/the-hcma/blumkin/issues/600)) ([7529ea9](https://github.com/the-hcma/blumkin/commit/7529ea93e2124cbe418697c420f536fec3cfd9f4))
* **tasks:** add tasks create, update, and delete skills ([#522](https://github.com/the-hcma/blumkin/issues/522)) ([aca9a20](https://github.com/the-hcma/blumkin/commit/aca9a205d306052903d4c5b5adb6591204987b53))
* **test:** run hermetic pytest with xdist -n auto ([#523](https://github.com/the-hcma/blumkin/issues/523)) ([2895272](https://github.com/the-hcma/blumkin/commit/2895272995425b06afc8d73a72d2fe8bb92fb52a))


### Bug Fixes

* **auth:** the Microsoft live test gate counts a vaulted client id, a… ([#740](https://github.com/the-hcma/blumkin/issues/740)) ([3041ad2](https://github.com/the-hcma/blumkin/commit/3041ad259ec19f9edc8df93ad50970c69becacf4))
* **browser:** a cell edit in the last day column commits and clears, … ([#689](https://github.com/the-hcma/blumkin/issues/689)) ([aaf75b5](https://github.com/the-hcma/blumkin/commit/aaf75b5d1d607caa407f41a9ec394d17030340c7))
* **browser:** a flow's pick step can say which of two identical rows it means ([#747](https://github.com/the-hcma/blumkin/issues/747)) ([1da455a](https://github.com/the-hcma/blumkin/commit/1da455ae313a3fdebecfadef4c7520e7c2a779d6))
* **browser:** a key press retries with a fresh vetted handle when the form re-renders ([#743](https://github.com/the-hcma/blumkin/issues/743)) ([41baa24](https://github.com/the-hcma/blumkin/commit/41baa24df644d4a58e555e4d3ef3a5eaabac0de6))
* **browser:** a swallowed form post is cancelled before the safety net is removed ([#745](https://github.com/the-hcma/blumkin/issues/745)) ([85b8e1a](https://github.com/the-hcma/blumkin/commit/85b8e1a0d9d9492303d13632642640c1ad618764))
* **browser:** an upload step finds a file input recorded as a button,… ([#703](https://github.com/the-hcma/blumkin/issues/703)) ([b438db7](https://github.com/the-hcma/blumkin/commit/b438db7f2a470abf7a0f01853d4d2db1192c90b5))
* **browser:** dialog waits for a loading list and retries its close ([#598](https://github.com/the-hcma/blumkin/issues/598)) ([#674](https://github.com/the-hcma/blumkin/issues/674)) ([64b04b4](https://github.com/the-hcma/blumkin/commit/64b04b4add8701cb99336d19094f16ec9bd9e444))
* **browser:** every driver call that reaches the page has a deadline ([#623](https://github.com/the-hcma/blumkin/issues/623)) ([#672](https://github.com/the-hcma/blumkin/issues/672)) ([8d0f4ee](https://github.com/the-hcma/blumkin/commit/8d0f4ee9e2f1967274750c4fa3e4e0ff2f42bb8d))
* **browser:** import playwright only when a browser command runs ([#521](https://github.com/the-hcma/blumkin/issues/521)) ([77c551a](https://github.com/the-hcma/blumkin/commit/77c551a65df4de413c652052414897a9da8dd52d))
* **browser:** model check compares link and button names with their numbers folded, so a moved count is not drift ([#626](https://github.com/the-hcma/blumkin/issues/626)) ([#660](https://github.com/the-hcma/blumkin/issues/660)) ([48028ee](https://github.com/the-hcma/blumkin/commit/48028ee7136b715e167b4e83c81710cf8754550a))
* **browser:** read cross-origin frames a fresh attach cannot see over their own DevTools socket ([#551](https://github.com/the-hcma/blumkin/issues/551)) ([#553](https://github.com/the-hcma/blumkin/issues/553)) ([378f403](https://github.com/the-hcma/blumkin/commit/378f40318455b4b623458cb9255e13892659b17b))
* **browser:** the safety net lets an upload's frame post through, and… ([#683](https://github.com/the-hcma/blumkin/issues/683)) ([4fe0d90](https://github.com/the-hcma/blumkin/commit/4fe0d90fca9a8b21e3a1a07c8fb2f371df3514fc))
* **calendar:** calendar view follows every page and says when the range was not read in full ([#655](https://github.com/the-hcma/blumkin/issues/655)) ([#665](https://github.com/the-hcma/blumkin/issues/665)) ([7a9bb97](https://github.com/the-hcma/blumkin/commit/7a9bb97da1cf6c28aeca0414d13b79e275b64840))
* **calendar:** include the organizer in suggest availability ([#477](https://github.com/the-hcma/blumkin/issues/477)) ([389aa6b](https://github.com/the-hcma/blumkin/commit/389aa6b7c7d97b024acbaa9fd557862fc6cb7507))
* **calendar:** moving a recurring OOO series onto its own occurrences is not refused ([#734](https://github.com/the-hcma/blumkin/issues/734)) ([82c9be7](https://github.com/the-hcma/blumkin/commit/82c9be78609d7d8d20a4b487dec2151087181732))
* **calendar:** the guard and calendar check resolve the signed-in organizer, not the profile email label ([#729](https://github.com/the-hcma/blumkin/issues/729)) ([da70dac](https://github.com/the-hcma/blumkin/commit/da70dacfde7b02aa92fc917f6a1adad88324c210))
* **calendar:** your own OOO block is a note, an invitee's stays a conflict, and multi-day blocks name their dates ([#728](https://github.com/the-hcma/blumkin/issues/728)) ([e28e1d6](https://github.com/the-hcma/blumkin/commit/e28e1d6a5e15130d3368e9f94647522749245f6c))
* **cli:** unknown --fields names suggest valid ones, --profile placement hint, skill notes on query syntax and warnings ([#593](https://github.com/the-hcma/blumkin/issues/593)) ([#620](https://github.com/the-hcma/blumkin/issues/620)) ([7dd47aa](https://github.com/the-hcma/blumkin/commit/7dd47aa0e161fe260f5b21c9d4317f6508fc58cb))
* **config:** pin the tables allowed next to named profiles and explain the 1.13.0 error ([#575](https://github.com/the-hcma/blumkin/issues/575)) ([#581](https://github.com/the-hcma/blumkin/issues/581)) ([dd9af65](https://github.com/the-hcma/blumkin/commit/dd9af6511af6c25e505d91d07deb96be9e0ac3b2))
* **deps:** batch updates including CVE fixes ([#451](https://github.com/the-hcma/blumkin/issues/451)) ([03557ea](https://github.com/the-hcma/blumkin/commit/03557ea76d8d88934ac52f57e2d056bb87b6e2ec))
* **deps:** batch updates including CVE fixes ([#455](https://github.com/the-hcma/blumkin/issues/455)) ([0f4e6ca](https://github.com/the-hcma/blumkin/commit/0f4e6ca052c4ca263bcf8167be8df02b79c2d2ed))
* **deps:** batch updates including CVE fixes ([#635](https://github.com/the-hcma/blumkin/issues/635)) ([dd3bfcf](https://github.com/the-hcma/blumkin/commit/dd3bfcf2bd96b9fc167fed0b7950d4d7fe3022f1))
* **deps:** batch updates including CVE fixes ([#680](https://github.com/the-hcma/blumkin/issues/680)) ([ccdadab](https://github.com/the-hcma/blumkin/commit/ccdadab1e2bb64eaaf51e18311d5207b55f572db))
* guard bash completion on old bash, and document and test the whole command surface ([#567](https://github.com/the-hcma/blumkin/issues/567)) ([#578](https://github.com/the-hcma/blumkin/issues/578)) ([559609a](https://github.com/the-hcma/blumkin/commit/559609ac76f7e5430dacae0765b228f3bc8effc6))
* launcher gate, worktree-aware mcp install, send-draft ids and private downloads ([#565](https://github.com/the-hcma/blumkin/issues/565)) ([#577](https://github.com/the-hcma/blumkin/issues/577)) ([3549a8c](https://github.com/the-hcma/blumkin/commit/3549a8c4fd72ca8902d67f239ae68e13f0c87ffb))
* **mail:** Gmail update-draft, delete-draft and send-draft accept the… ([#721](https://github.com/the-hcma/blumkin/issues/721)) ([2e85069](https://github.com/the-hcma/blumkin/commit/2e85069efda8457644f786eabf68f8b80e3f4a19))
* **mail:** reply and forward keep an HTML or Markdown body above the quoted original ([#612](https://github.com/the-hcma/blumkin/issues/612)) ([#631](https://github.com/the-hcma/blumkin/issues/631)) ([bce88dc](https://github.com/the-hcma/blumkin/commit/bce88dcebed70c866fa610b862c8ce3d8b233b34))
* **mcp:** refuse tool arguments the schema does not declare, with hints ([#456](https://github.com/the-hcma/blumkin/issues/456)) ([#528](https://github.com/the-hcma/blumkin/issues/528)) ([4b10470](https://github.com/the-hcma/blumkin/commit/4b10470b52e69f74319fd4afffb95442cc298702))
* **mcp:** serve the handshake era only so Copilot CLI can connect ([#563](https://github.com/the-hcma/blumkin/issues/563)) ([#571](https://github.com/the-hcma/blumkin/issues/571)) ([93c76d6](https://github.com/the-hcma/blumkin/commit/93c76d60fb43f426e374dcfbf201fd3847d39fad))
* **packaging:** keep the Rust agent's cargo output out of the sdist and test that it stays out ([#739](https://github.com/the-hcma/blumkin/issues/739)) ([36b2832](https://github.com/the-hcma/blumkin/commit/36b28325438027c89ad13d86fd4a902485d9ec6e))
* **people:** a consumer Gmail's 400 'Must be a G Suite domain user' is no directory, so contacts still resolve ([#733](https://github.com/the-hcma/blumkin/issues/733)) ([38a1c49](https://github.com/the-hcma/blumkin/commit/38a1c493f4f38e5a9d4ec279c26700a451ff4c06))
* **receipts:** a receipt PDF is read with the photo pipeline and a repeated card amount backs a garbled total ([#677](https://github.com/the-hcma/blumkin/issues/677)) ([#678](https://github.com/the-hcma/blumkin/issues/678)) ([403f1e4](https://github.com/the-hcma/blumkin/commit/403f1e4fe189fc8c675e5761f579d4ba0a704267))
* **receipts:** photo receipts read right with EXIF turn, tip tables, stray currency and screenshot amounts ([#627](https://github.com/the-hcma/blumkin/issues/627)) ([#676](https://github.com/the-hcma/blumkin/issues/676)) ([e3c3e5e](https://github.com/the-hcma/blumkin/commit/e3c3e5e6598484440224d7a37466a4c69d553456))
* stable Gmail attachment ids, Microsoft mail thread and launcher fixes ([#564](https://github.com/the-hcma/blumkin/issues/564)) ([#572](https://github.com/the-hcma/blumkin/issues/572)) ([ee3fba0](https://github.com/the-hcma/blumkin/commit/ee3fba099539bb66be3408ee10fa5cf77b0bee37))
* **tasks:** showing a task with no usable prompt is an error, and listing flags it ([#615](https://github.com/the-hcma/blumkin/issues/615)) ([#634](https://github.com/the-hcma/blumkin/issues/634)) ([4ffa2ba](https://github.com/the-hcma/blumkin/commit/4ffa2ba0cf1ccb533853ec3f69a42d0c9df75199))


### Documentation

* agent onboarding guide for running blumkin from the source tree ([#566](https://github.com/the-hcma/blumkin/issues/566)) ([#568](https://github.com/the-hcma/blumkin/issues/568)) ([ea4c71c](https://github.com/the-hcma/blumkin/commit/ea4c71ce2269a692f127ded348537f1bbafed0e6))
* **agents:** point the merge instructions at scripts/dev/merge-stack ([#557](https://github.com/the-hcma/blumkin/issues/557)) ([204743e](https://github.com/the-hcma/blumkin/commit/204743e1c8a4184b01f99db1033a6c6b472dfb7b))
* **browser:** guide, security notes and agent docs for browser tasks ([#495](https://github.com/the-hcma/blumkin/issues/495)) ([b29538c](https://github.com/the-hcma/blumkin/commit/b29538c235c5cd3477f74d5e75f499b100b5ab96))
* describe blumkin as Microsoft 365 and Google Workspace, work or personal ([#533](https://github.com/the-hcma/blumkin/issues/533)) ([#560](https://github.com/the-hcma/blumkin/issues/560)) ([dd64b78](https://github.com/the-hcma/blumkin/commit/dd64b78238374514579679f6f0c187359caac820))
* **loop:** guide, skill and security notes for loop/cron ([#507](https://github.com/the-hcma/blumkin/issues/507)) ([#512](https://github.com/the-hcma/blumkin/issues/512)) ([f20d064](https://github.com/the-hcma/blumkin/commit/f20d0649a1f63e0113562d38be74f857ee35bfaa))
* **mail:** name the folders Google accepts in help, skill summary an… ([#463](https://github.com/the-hcma/blumkin/issues/463)) ([7bd3896](https://github.com/the-hcma/blumkin/commit/7bd3896f02ed23599495032d35f8e281006316e9))
* **skill:** fix Google mail folder names ([#459](https://github.com/the-hcma/blumkin/issues/459)) ([e55d61c](https://github.com/the-hcma/blumkin/commit/e55d61cf7beef7be1f7637cf1e159f6a99acff9c))
* **skill:** invite attendees after finding a mutual slot ([#457](https://github.com/the-hcma/blumkin/issues/457)) ([e81a520](https://github.com/the-hcma/blumkin/commit/e81a520e33fd2aadc99933442969fae730e3a307))
* **skills:** expense-report follows the user's task definition (SSO tile click-through, report discovery, upload order) ([#609](https://github.com/the-hcma/blumkin/issues/609)) ([7acfa3d](https://github.com/the-hcma/blumkin/commit/7acfa3de5ffe10dea8fd0dc7fa23da0e6766c3fb))
* **skills:** time-sheet opens its form_url and clicks the work accou… ([#681](https://github.com/the-hcma/blumkin/issues/681)) ([fe2803b](https://github.com/the-hcma/blumkin/commit/fe2803b5bab1ffe80805977ccb69c54bcd98fd5f))
* teach expense and time-sheet skills the incremental ledger workflow ([#519](https://github.com/the-hcma/blumkin/issues/519)) ([5fdfd7d](https://github.com/the-hcma/blumkin/commit/5fdfd7d0de33cf4d4bf56f923ab8f65841643d20))

## [1.13.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.12.1...blumkin-v1.13.0) (2026-10-20)


### Features

* **auth:** configurable keychain unlock via security unlock-keychain… ([#445](https://github.com/the-hcma/blumkin/issues/445)) ([b872627](https://github.com/the-hcma/blumkin/commit/b87262740a6ff4c710c1532ab1671f2d1df515f0))


### Documentation

* merge path is gh pr merge --auto; the queue sets the strategy ([#447](https://github.com/the-hcma/blumkin/issues/447)) ([263b3dd](https://github.com/the-hcma/blumkin/commit/263b3dd981faa06f0a4d468dd67eb1bbc4b8f134))

## [1.12.1](https://github.com/the-hcma/blumkin/compare/blumkin-v1.12.0...blumkin-v1.12.1) (2026-10-18)


### Bug Fixes

* **deps:** batch updates including CVE fixes ([#418](https://github.com/the-hcma/blumkin/issues/418)) ([a5081c5](https://github.com/the-hcma/blumkin/commit/a5081c5601a83ed5e156d8994c6ce9f81ab02be8))
* **deps:** batch updates including CVE fixes ([#419](https://github.com/the-hcma/blumkin/issues/419)) ([cce90f8](https://github.com/the-hcma/blumkin/commit/cce90f80daac0d7af856e92ba5ea0b2234b5a5ba))
* **workflows:** look up PRs by head ref in cleanup-merged-branches ([#433](https://github.com/the-hcma/blumkin/issues/433)) ([ede6e90](https://github.com/the-hcma/blumkin/commit/ede6e907af516905c51562fbae2f99998a0cab4c))


### Documentation

* sync agent rule template fixes from repository-helpers ([#417](https://github.com/the-hcma/blumkin/issues/417)) ([f762514](https://github.com/the-hcma/blumkin/commit/f762514e93a45731b0f74b460f624e8cf8d89242))

## [1.12.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.11.0...blumkin-v1.12.0) (2026-09-28)


### Features

* enforce profile-scoped outbound message policy ([#415](https://github.com/the-hcma/blumkin/issues/415)) ([f153f95](https://github.com/the-hcma/blumkin/commit/f153f95a6004c45c6c8d9a6725d46d68692ec19a))

## [1.11.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.10.0...blumkin-v1.11.0) (2026-09-28)


### Features

* detect orphaned agents and stale mcp serve processes ([#408](https://github.com/the-hcma/blumkin/issues/408)) ([#409](https://github.com/the-hcma/blumkin/issues/409)) ([0609cb6](https://github.com/the-hcma/blumkin/commit/0609cb6bf0ae07cff3b079f6fa75f0ed0adc1e88))


### Documentation

* sync unwrapped agent rule templates from repository-helpers ([#410](https://github.com/the-hcma/blumkin/issues/410)) ([405f738](https://github.com/the-hcma/blumkin/commit/405f7385773090c65ed260a447ef76d8a6ceef3c))

## [1.10.0](https://github.com/the-hcma/blumkin/compare/blumkin-v1.9.2...blumkin-v1.10.0) (2026-09-27)


### Features

* **agent:** flag version skew in status, add agent stop ([#404](https://github.com/the-hcma/blumkin/issues/404)) ([1712877](https://github.com/the-hcma/blumkin/commit/1712877a09d365cb83dcc073637e8cd8bf20e79c))


### Bug Fixes

* **agent:** resolve runtime dir independent of $TMPDIR ([#403](https://github.com/the-hcma/blumkin/issues/403)) ([c9338f0](https://github.com/the-hcma/blumkin/commit/c9338f077a195dcee8781a86d4353568cf6f9db5))


### Documentation

* refresh local github-api-throttle summary to match canonical ([#399](https://github.com/the-hcma/blumkin/issues/399)) ([5b81615](https://github.com/the-hcma/blumkin/commit/5b8161514b96ecb4e418bb113a4bcbfd8daea9c8))

## [1.9.2](https://github.com/the-hcma/blumkin/compare/blumkin-v1.9.1...blumkin-v1.9.2) (2026-09-25)


### Bug Fixes

* dedup attachments by content, not Graph's unreliable size metadata ([#392](https://github.com/the-hcma/blumkin/issues/392)) ([#396](https://github.com/the-hcma/blumkin/issues/396)) ([dd3ca3e](https://github.com/the-hcma/blumkin/commit/dd3ca3efe025459f9a1a05bf5dde676e8b84eae5))

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
