# Changelog

All notable changes to Foreman are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are intentional: release-please maintains a rolling release PR from
conventional commits, and merging it cuts the tag, GitHub release, and this
changelog (`task release:*` remains as a manual override).
## [2.8.1](https://github.com/ponderousdev/foreman/compare/v2.8.0...v2.8.1) (2026-08-31)


### Bug Fixes

* sync harmon-devkit skills to v0.37.0 ([#186](https://github.com/ponderousdev/foreman/issues/186)) ([b9a8ab3](https://github.com/ponderousdev/foreman/commit/b9a8ab3503d2e4c0017a9b2e5c5573bab84c7138))

## [2.8.0](https://github.com/ponderousdev/foreman/compare/v2.7.0...v2.8.0) (2026-08-24)


### Features

* **pm:** fit label registry to foreman — domains, areas ([#176](https://github.com/ponderousdev/foreman/issues/176)) ([2c39671](https://github.com/ponderousdev/foreman/commit/2c396717eac0df228f80ad960eab3fc8aa98b425))


### Bug Fixes

* sync harmon-devkit skills to v0.35.0 ([#177](https://github.com/ponderousdev/foreman/issues/177)) ([547423d](https://github.com/ponderousdev/foreman/commit/547423d97bae7df454ed8a2f8345feddd5cbd5a6))

## [2.7.0](https://github.com/ponderousdev/foreman/compare/v2.6.0...v2.7.0) (2026-08-22)


### Features

* **image:** version and digest-pin the agent image ([#39](https://github.com/ponderousdev/foreman/issues/39)) ([#172](https://github.com/ponderousdev/foreman/issues/172)) ([84ae47f](https://github.com/ponderousdev/foreman/commit/84ae47f357b9f5d41611c5bf8e91e30fe66efca4))

## [2.6.0](https://github.com/ponderousdev/foreman/compare/v2.5.0...v2.6.0) (2026-08-21)


### Features

* **backend:** surface a live runner reference in the agent-running narration ([#167](https://github.com/ponderousdev/foreman/issues/167)) ([f5d3c88](https://github.com/ponderousdev/foreman/commit/f5d3c88fa79d67c42e257524d66acc7e30fe2949)), closes [#126](https://github.com/ponderousdev/foreman/issues/126)
* **cli:** add title logo and 60fps braille spinner ([#168](https://github.com/ponderousdev/foreman/issues/168)) ([944654b](https://github.com/ponderousdev/foreman/commit/944654b9bef404443b7c119313a0c67cdfc5a238))


### Bug Fixes

* **test:** make test_local_runner pass on macOS (realpath + /proc gating) ([#165](https://github.com/ponderousdev/foreman/issues/165)) ([f018f69](https://github.com/ponderousdev/foreman/commit/f018f69568438cac99275f99b363fab8c2d8e4da)), closes [#154](https://github.com/ponderousdev/foreman/issues/154)

## [2.5.0](https://github.com/ponderousdev/foreman/compare/v2.4.0...v2.5.0) (2026-08-08)


### Features

* **backend:** extend responsive-dispatch liveness to vet and shepherd ([#161](https://github.com/ponderousdev/foreman/issues/161)) ([8b429e5](https://github.com/ponderousdev/foreman/commit/8b429e5f393b4ffe9384f6ddb4c71aa8ed20c318)), closes [#125](https://github.com/ponderousdev/foreman/issues/125)

## [2.4.0](https://github.com/ponderousdev/foreman/compare/v2.3.0...v2.4.0) (2026-08-08)


### Features

* **backend:** add Claude Code GLM adapter ([#153](https://github.com/ponderousdev/foreman/issues/153)) ([bf51564](https://github.com/ponderousdev/foreman/commit/bf5156434fefdfd6f171d432d25b46b11f8df99b))
* **backend:** add Claude Code Kimi adapter ([#151](https://github.com/ponderousdev/foreman/issues/151)) ([df5d8b9](https://github.com/ponderousdev/foreman/commit/df5d8b912de34037676743f22210487692a97d9d))
* **backend:** add Codex CLI adapter ([#155](https://github.com/ponderousdev/foreman/issues/155)) ([6ff8715](https://github.com/ponderousdev/foreman/commit/6ff8715237c0ed6fd8da9317fba97c9935570489))


### Documentation

* explain adapter attach capability and two-step execute flow ([#147](https://github.com/ponderousdev/foreman/issues/147)) ([#160](https://github.com/ponderousdev/foreman/issues/160)) ([a8ff0c2](https://github.com/ponderousdev/foreman/commit/a8ff0c2028d582ba61940792b38a6048b34a3040))

## [2.3.0](https://github.com/ponderousdev/foreman/compare/v2.2.0...v2.3.0) (2026-08-08)


### Features

* **backend:** add Claude Code DeepSeek adapter ([#145](https://github.com/ponderousdev/foreman/issues/145)) ([2b13572](https://github.com/ponderousdev/foreman/commit/2b13572627c90364bbe7edbc81c9bfb8ab0b111e))

## [2.2.0](https://github.com/ponderousdev/foreman/compare/v2.1.1...v2.2.0) (2026-08-08)


### Features

* **dispatch:** responsive dispatch — ack, liveness, phase updates ([#124](https://github.com/ponderousdev/foreman/issues/124)) ([ae7e59a](https://github.com/ponderousdev/foreman/commit/ae7e59a5d0c2c0f5c06e28f562f989ccaf830f9f))
* **github:** make Foreman PRs draft-first ([#143](https://github.com/ponderousdev/foreman/issues/143)) ([e543450](https://github.com/ponderousdev/foreman/commit/e5434505bb3679b55a00a82cee1c0078f41c3b43))
* **github:** namespace Foreman PR labels ([#141](https://github.com/ponderousdev/foreman/issues/141)) ([43bc6d3](https://github.com/ponderousdev/foreman/commit/43bc6d3760ab77c8b25cbf7da2c671dc52e10525))
* **report:** evolve unit status comment into snapshot + event log ([#82](https://github.com/ponderousdev/foreman/issues/82)) ([#120](https://github.com/ponderousdev/foreman/issues/120)) ([46fcbcf](https://github.com/ponderousdev/foreman/commit/46fcbcf26748be674ee8d9d84ace9e8335c9ac7e))
* **shepherd:** add current-head reviewer gate ([#144](https://github.com/ponderousdev/foreman/issues/144)) ([dfed3c3](https://github.com/ponderousdev/foreman/commit/dfed3c3349591bda70c11146e36d034c6b457800))
* **watch:** surface PR states in heartbeat (open/ready/escalated) ([#121](https://github.com/ponderousdev/foreman/issues/121)) ([51eef45](https://github.com/ponderousdev/foreman/commit/51eef45a595dd3a260f2b33bbc5fd67af9f01a56)), closes [#98](https://github.com/ponderousdev/foreman/issues/98)


### Bug Fixes

* **shepherd:** unify readiness gate ([#142](https://github.com/ponderousdev/foreman/issues/142)) ([2b76dc0](https://github.com/ponderousdev/foreman/commit/2b76dc0896781960e23edd1cfd340d90b5fa6cee))

## [2.1.1](https://github.com/ponderousdev/foreman/compare/v2.1.0...v2.1.1) (2026-08-04)


### Bug Fixes

* **ci:** least-privilege workflow hardening for public exposure ([#110](https://github.com/ponderousdev/foreman/issues/110)) ([d8987e2](https://github.com/ponderousdev/foreman/commit/d8987e2eeab15eec265d9dfa590b13ff4a600aad)), closes [#108](https://github.com/ponderousdev/foreman/issues/108)
* genericize shipped identifiers + permanent leakage guard ([#111](https://github.com/ponderousdev/foreman/issues/111)) ([be18246](https://github.com/ponderousdev/foreman/commit/be1824685253dd3d67dcccecf57b4c15c6fe63d4))
* relicense under Apache-2.0 ([#117](https://github.com/ponderousdev/foreman/issues/117)) ([2b2a4c3](https://github.com/ponderousdev/foreman/commit/2b2a4c31b1ebc9dcf43a9e73e9fa28b487f39099))


### Documentation

* record the D4 public-dispatch refusal in config + README ([#118](https://github.com/ponderousdev/foreman/issues/118)) ([893dc47](https://github.com/ponderousdev/foreman/commit/893dc4738b6c6b81d9d8d4c00328c5cb7c26401e))

## [2.1.0](https://github.com/ponderousdev/foreman/compare/v2.0.0...v2.1.0) (2026-08-03)


### Features

* **status:** print an overall snapshot when no target is given ([#88](https://github.com/ponderousdev/foreman/issues/88)) ([af372e6](https://github.com/ponderousdev/foreman/commit/af372e62ad81aa842d89a59edcc104973755a6cc))


### Bug Fixes

* **ci:** refresh uv.lock on the release PR automatically ([#62](https://github.com/ponderousdev/foreman/issues/62)) ([90df8b3](https://github.com/ponderousdev/foreman/commit/90df8b3649d4c8e4de62bb593ccd21a7f6492a53))
* **config:** keep dogfood top-level keys above the [verify] section ([#67](https://github.com/ponderousdev/foreman/issues/67)) ([2654158](https://github.com/ponderousdev/foreman/commit/26541584dbd53a941fc1a63e796f135ba8462273))
* **github:** derive CI state from PAT-readable sources ([#89](https://github.com/ponderousdev/foreman/issues/89)) ([#97](https://github.com/ponderousdev/foreman/issues/97)) ([a20f518](https://github.com/ponderousdev/foreman/commit/a20f5185b7289ca44fc85daaebaa6653275abd2b))
* **github:** derive PR merged-ness — gh dropped the boolean field ([#86](https://github.com/ponderousdev/foreman/issues/86)) ([2706c85](https://github.com/ponderousdev/foreman/commit/2706c85531113d66c4a754d5defb7bbdda89fc48))
* **github:** read comment body from stdin, not literal '@-' ([#90](https://github.com/ponderousdev/foreman/issues/90)) ([#93](https://github.com/ponderousdev/foreman/issues/93)) ([8f6d722](https://github.com/ponderousdev/foreman/commit/8f6d7225644e814ff08a5172926b5f4c7807bde0))
* **graph:** normalize every gh relational field, not just subIssues ([#78](https://github.com/ponderousdev/foreman/issues/78)) ([568901f](https://github.com/ponderousdev/foreman/commit/568901f32943a73b400ae641b26edf56bc4290c1))
* **graph:** normalize gh's subIssues connection-object shape ([#74](https://github.com/ponderousdev/foreman/issues/74)) ([df492ca](https://github.com/ponderousdev/foreman/commit/df492ca837035d257499e31a93758beb26592597))
* **inputs:** close the vet/status-comment/title gaps and correct the input-surface table ([#64](https://github.com/ponderousdev/foreman/issues/64)) ([0a290dd](https://github.com/ponderousdev/foreman/commit/0a290dd88e1e7cfed3f85bb3e810d0643acd2f9a))
* **shepherd:** pick off the security-first [#54](https://github.com/ponderousdev/foreman/issues/54) hardening items ([#70](https://github.com/ponderousdev/foreman/issues/70)) ([fe5f281](https://github.com/ponderousdev/foreman/commit/fe5f28132dd79153feea962573ed7e8f2475f485))
* **signatures:** classify docker-daemon failures as environmental ([#65](https://github.com/ponderousdev/foreman/issues/65)) ([5914bfb](https://github.com/ponderousdev/foreman/commit/5914bfb156a178201c7786efdd162b7098776b4f))
* **status:** fold local-run evidence into targeted status ([#94](https://github.com/ponderousdev/foreman/issues/94)) ([#96](https://github.com/ponderousdev/foreman/issues/96)) ([c146217](https://github.com/ponderousdev/foreman/commit/c146217bac859939eb340e836f19c0ffb7b9c016))

## 2.0.0 (2026-08-01)


### Features

* **dispatch:** drive units through the runner seam with crash-safe reattach ([63fd964](https://github.com/ponderousdev/foreman/commit/63fd964509c55815ca8e7569fb5355ce8711a076))
* foreman v1 — deterministic supervisor for milestone-driven agent dispatch ([#277](https://github.com/ponderousdev/foreman/issues/277)) ([3733e20](https://github.com/ponderousdev/foreman/commit/3733e20d3bbc84c4ee3cc31223b9135bf805386d))
* Foreman v2.0 — extraction, the Runner seam, and security controls ([ec0ad3d](https://github.com/ponderousdev/foreman/commit/ec0ad3d37d47d71dabcb0d7cbe977576715017a3))
* import foreman v1 from harmon-init with preserved history ([1caebfa](https://github.com/ponderousdev/foreman/commit/1caebfa9cf35b31817f32b36425cd7f58386be11)), closes [#10](https://github.com/ponderousdev/foreman/issues/10)
* **preflight:** empirical security gate, input surfaces, and local triage ([ecf11b1](https://github.com/ponderousdev/foreman/commit/ecf11b1aa3768287b34db061124d5eb0785747bb))
* **runner:** add LocalRunner with a status-recording spawn wrapper ([4d11bf7](https://github.com/ponderousdev/foreman/commit/4d11bf76b679dbf8b4c986c7ddefa5fb6d661536))
* **runner:** define the Runner protocol, handle store, and leak test ([630a251](https://github.com/ponderousdev/foreman/commit/630a251e45a999838ac353e3e7cf7fdf3ff1a288)), closes [#19](https://github.com/ponderousdev/foreman/issues/19)
* stand up the uv package skeleton and rename preflight to vet ([036c048](https://github.com/ponderousdev/foreman/commit/036c048a76737b685848907295c6d6d06f43ca06)), closes [#10](https://github.com/ponderousdev/foreman/issues/10)
* **trust:** capability model, D4/D13 trust gating, and the composed verify gate ([40d33fa](https://github.com/ponderousdev/foreman/commit/40d33fae5fd91302bc070daf8c1dc9af92d88afc))


### Bug Fixes

* address CodeRabbit review on the v2.0 PR ([082d745](https://github.com/ponderousdev/foreman/commit/082d745b88fda33b6e8f1d352abbf42e3ce6d3c1))
* address PR review findings ([9d0f229](https://github.com/ponderousdev/foreman/commit/9d0f22997cc8036b83c3341cd0e27e749c2dd947))
* address review feedback ([e99f6b6](https://github.com/ponderousdev/foreman/commit/e99f6b6b45ff66ba4f7f0b8ad4c4e69bb3f8dbc0))
* close the three real v2.0 gaps ([#14](https://github.com/ponderousdev/foreman/issues/14) trust re-eval, [#16](https://github.com/ponderousdev/foreman/issues/16) examples, [#20](https://github.com/ponderousdev/foreman/issues/20) git-through-runner) ([c262529](https://github.com/ponderousdev/foreman/commit/c262529a13a6a0c54c438a990270d5b6b706d2a8))
* **devcontainer:** grant Claude access to Harmon repos ([8b219c0](https://github.com/ponderousdev/foreman/commit/8b219c0e0425c9034ae49f800e71a4359111cf05))
* **devcontainer:** grant Claude access to Harmon repos ([effc2b1](https://github.com/ponderousdev/foreman/commit/effc2b1362958e27dd22710a514cc86f5c791f9e))
* **devcontainer:** manage FOREMAN_AGENT_GH_TOKEN in the bot env allow-list ([2cbd1e0](https://github.com/ponderousdev/foreman/commit/2cbd1e07a186c4d97afdf409b8cda13654c96031))
* omit unused project automation ([2ec2b2c](https://github.com/ponderousdev/foreman/commit/2ec2b2c7d3225c763b38a82e97f33a569fb65000))
* plumb the agent read-only token and close verification doc gaps ([60c5ea6](https://github.com/ponderousdev/foreman/commit/60c5ea603f17658acabc8855aac5ad66648b604b))
* restore project automation workflow ([2c9da01](https://github.com/ponderousdev/foreman/commit/2c9da010762161adb237f6ea5c28fe7ac29e51bb))
* **shepherd:** enforce origin-trust inheritance and route adjudication writes through foreman ([8cfa9b8](https://github.com/ponderousdev/foreman/commit/8cfa9b8906fbdb768af7137a1da4acf877488741))
* **shepherd:** harden the adjudication write path per Codex review ([cf43898](https://github.com/ponderousdev/foreman/commit/cf4389811fc899cb0bf79a4e6e46812880986eb5))
* **shepherd:** key reply dedupe on a stable marker, not note text ([969e568](https://github.com/ponderousdev/foreman/commit/969e5685faef129517e4ba2c93cb945e7edc1848))
* **shepherd:** origin-trust inheritance + foreman-performed adjudication writes ([dbb5ece](https://github.com/ponderousdev/foreman/commit/dbb5ece11d469a9609322338a3c1e0f956d0a604))
* **trust:** re-evaluate the D4/D13 predicate at dispatch ([#14](https://github.com/ponderousdev/foreman/issues/14)) ([1b50d19](https://github.com/ponderousdev/foreman/commit/1b50d193b0b92ac060b582768557183f4ef10262))


### Documentation

* add Foreman v2 spec ([850cc01](https://github.com/ponderousdev/foreman/commit/850cc012207d089f57079e003ff20529ef689abc))
* add Foreman v2 spec ([281b7a6](https://github.com/ponderousdev/foreman/commit/281b7a6c4041f7c98c9aeb832876a4abb5ae9b9c))
* address CodeRabbit on the docs PR ([3f02d90](https://github.com/ponderousdev/foreman/commit/3f02d903301264395dc15c7990fb9447ee28a210)), closes [#49](https://github.com/ponderousdev/foreman/issues/49)
* address CodeRabbit review on the spec amendments ([0736347](https://github.com/ponderousdev/foreman/commit/0736347041f60c6371211724a83a8cfa3a93f40d))
* **agents:** correct stale v1 references after the v2 extraction ([e64c920](https://github.com/ponderousdev/foreman/commit/e64c9209d86d80a78d6dbe060e36fee3c06c6533))
* amend foreman-v2 spec from the critical design review ([3581068](https://github.com/ponderousdev/foreman/commit/3581068e1b2e286468823b0eb75ea49eda1bbbb8))
* amend foreman-v2 spec from the critical design review ([04975f8](https://github.com/ponderousdev/foreman/commit/04975f86f9087588b772ea596536c16db8146b3b))
* close the round-two token-doc gaps per Codex review ([0d5c13c](https://github.com/ponderousdev/foreman/commit/0d5c13c1fff4f9aa3451732b64a0c07a919c4d22))
* complete the agent-token sweep per Codex review ([bdf00fc](https://github.com/ponderousdev/foreman/commit/bdf00fc16dd91056ba842182da1dda7f301526ab))
* correct D11's prerequisite — two grants, and the bot has neither ([d00536e](https://github.com/ponderousdev/foreman/commit/d00536e270439f1eb8549602fea1a9c3806b675b))
* cover every preflight assertion in the acceptance criteria ([ce02d13](https://github.com/ponderousdev/foreman/commit/ce02d13ec3aa6cb18401e7360449dfce69e00c07))
* document the bot PAT — permissions, layering, and how to create one ([11fcbec](https://github.com/ponderousdev/foreman/commit/11fcbecb824cb208c13ee606a52e30ce5db240ba))
* document the bot PAT — permissions, layering, and how to create one ([c0573be](https://github.com/ponderousdev/foreman/commit/c0573be4e293b9c1161e96d39d6c50b942015f1d))
* don't overclaim the private-to-public transition in D11 ([a886267](https://github.com/ponderousdev/foreman/commit/a886267024b54d717762b99e7cb779a4c306397a))
* drop applied migration scaffolding from the v2 spec ([8c4928d](https://github.com/ponderousdev/foreman/commit/8c4928dffb9ae7941c01964e50c4f1ce065684cc))
* drop applied migration scaffolding from the v2 spec ([cd22090](https://github.com/ponderousdev/foreman/commit/cd220905ae716da64e36f3ea5a189435dffda961))
* drop the Checks permission and stop duplicating the PAT table ([28b078c](https://github.com/ponderousdev/foreman/commit/28b078cb216135bd19f9ece7b7c442ed34c7885b))
* **examples:** add marked stack-specific composed-gate illustrations ([#16](https://github.com/ponderousdev/foreman/issues/16)) ([98afff4](https://github.com/ponderousdev/foreman/commit/98afff496827834dad39b04effcac4b57f121065))
* fix overclaimed invariants in the Foreman v2 spec ([b5b32ee](https://github.com/ponderousdev/foreman/commit/b5b32eef2092c04c250723c7f06d772d760fb905))
* Foreman-specific README with usage + setup, and real product docs ([f6d0fab](https://github.com/ponderousdev/foreman/commit/f6d0fab8e1724d768873f8c3dd6bf3ffc4726a38))
* foreman-v2 second-review amendments (D13 gating model, D14 immutable tags, input surfaces) ([799ea80](https://github.com/ponderousdev/foreman/commit/799ea807fb4ffb919a9e21a2b20005f13f17fa53))
* **foreman:** reword prompt-injection surface to trusted_actors ([9c84154](https://github.com/ponderousdev/foreman/commit/9c84154aba2c67474ddd7cc26af2d511c10046cf)), closes [#10](https://github.com/ponderousdev/foreman/issues/10)
* harmon-init lives at evanharmon1/harmon-init ([287986d](https://github.com/ponderousdev/foreman/commit/287986dc8425f24a24f001c9f641bda6e3a3debc))
* make the README and product docs Foreman-specific ([718e62d](https://github.com/ponderousdev/foreman/commit/718e62d3615cba521d6e89c7365331b22f3a488f))
* qualify v1 code citations in the Foreman v2 spec ([b6d24dc](https://github.com/ponderousdev/foreman/commit/b6d24dcf05ec9e773690cad5b05a6b5c6377faf1))
* record the extraction method and the bypass-actor audit procedure ([63a61cf](https://github.com/ponderousdev/foreman/commit/63a61cf992868b6faf007cb4d57c2364d4e43080))
* settle the distribution question as D11 ([28ed554](https://github.com/ponderousdev/foreman/commit/28ed55485747b795ab7fb91ed7c9e5418f86d82b))
* settle the distribution question as D11 ([a75b783](https://github.com/ponderousdev/foreman/commit/a75b7834c3d09bef8d48f2f35a48f1ea57435d97))
* **spec:** address CodeRabbit review on the second-review amendments ([a9df7c6](https://github.com/ponderousdev/foreman/commit/a9df7c64cf8e6b181b0a546011dc3f1c08e77cef))
* **spec:** amend foreman-v2 from the second design review ([c72a4e6](https://github.com/ponderousdev/foreman/commit/c72a4e62318fd6043eb1397e11d2452a08971d13))
* sprite advertises untrusted-input alongside ports ([4c0230e](https://github.com/ponderousdev/foreman/commit/4c0230e1c12c301b04cf83a146ed894d17556821))
* stop overclaiming what the bot profile denies ([9e07118](https://github.com/ponderousdev/foreman/commit/9e0711859dbb6f98687fcf409e7819b1dcd00517))
* the harmon-init vocabulary counterpart is a change, not an issue ([e26d3c3](https://github.com/ponderousdev/foreman/commit/e26d3c3c6834b3b30be93b304649e838f08a9ce9))
* update architecture, DESIGN, vocabulary, and add the migration guide ([62b7d1f](https://github.com/ponderousdev/foreman/commit/62b7d1fc978a1c639c56760195bfc18936c8471b))


### Continuous Integration

* gate PRs on pytest, ruff, and mypy; release as a python package ([b9fd4e3](https://github.com/ponderousdev/foreman/commit/b9fd4e399f8c233ddcc202fd5dc5e13c72631a70))

## [Unreleased]

### Added

- Initial repository scaffolding generated from [harmon-init](https://github.com/evanharmon1/harmon-init) on 2026-07-15.
