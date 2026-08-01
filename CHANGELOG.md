# Changelog

All notable changes to Foreman are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are intentional: release-please maintains a rolling release PR from
conventional commits, and merging it cuts the tag, GitHub release, and this
changelog (`task release:*` remains as a manual override).
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
