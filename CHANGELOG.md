## [1.1.1](https://github.com/mechemsi/selfhosted-runner-swarm/compare/v1.1.0...v1.1.1) (2026-10-04)

### Bug Fixes

* **server:** serve the API with waitress instead of the Flask dev server ([#27](https://github.com/mechemsi/selfhosted-runner-swarm/issues/27)) ([2ee4d67](https://github.com/mechemsi/selfhosted-runner-swarm/commit/2ee4d67bb019a5381280c59f461ae6532fcc9969))

## [1.1.0](https://github.com/mechemsi/selfhosted-runner-swarm/compare/v1.0.0...v1.1.0) (2026-10-04)

### Features

* publish images to GHCR on release and pull them on the host ([#26](https://github.com/mechemsi/selfhosted-runner-swarm/issues/26)) ([949d358](https://github.com/mechemsi/selfhosted-runner-swarm/commit/949d35864e03cfba4eb1a9fad333ca1330e9ec9c))

## 1.0.0 (2026-10-04)

### Features

* add max_runner_lifetime backstop to reap leaked/hung runners ([0dc3fa6](https://github.com/mechemsi/selfhosted-runner-swarm/commit/0dc3fa61756f084ac34cb310c956b1688ec5288e))
* add max_total_runners global ceiling across all pools ([f62f5ad](https://github.com/mechemsi/selfhosted-runner-swarm/commit/f62f5ad7249661520f7ff0be5c56b6f89c09ead4))
* harden the API for network exposure — scoped tokens, brute-force guard, stable token ([#5](https://github.com/mechemsi/selfhosted-runner-swarm/issues/5)) ([12d1c3f](https://github.com/mechemsi/selfhosted-runner-swarm/commit/12d1c3f38e09b2262883bd15e9170596cb9fc5d8))
* MariaDB store and a Next.js gateway so the dashboard can face the network ([#6](https://github.com/mechemsi/selfhosted-runner-swarm/issues/6)) ([d3acae1](https://github.com/mechemsi/selfhosted-runner-swarm/commit/d3acae15fd14f1f19653245d97fe53b99ced37c5))
* release with semantic-release and report the running version ([#25](https://github.com/mechemsi/selfhosted-runner-swarm/issues/25)) ([334b8cc](https://github.com/mechemsi/selfhosted-runner-swarm/commit/334b8cc4fa84c29729aa23a94ed3d779a2a932b0))
* support personal account runner pools ([8cff22c](https://github.com/mechemsi/selfhosted-runner-swarm/commit/8cff22c68f0b8dae7dc7b109b97c5d6bd3146d1f))

### Bug Fixes

* build the runner image for the host's docker GID ([6492297](https://github.com/mechemsi/selfhosted-runner-swarm/commit/6492297e4c60274a42da8b67ff790a3d4670ccc8))
* **docker:** prune networks no container uses, every tick ([#19](https://github.com/mechemsi/selfhosted-runner-swarm/issues/19)) ([32cedfa](https://github.com/mechemsi/selfhosted-runner-swarm/commit/32cedfadcb060c09fd2c9a85092a58848461c336))
* enforce GitHub API rate budget ([8aa76f0](https://github.com/mechemsi/selfhosted-runner-swarm/commit/8aa76f0cc498df616ea1e39d06f3397f86b74390))
* flag incomplete queue scans; keep typed pool limits on a failed save ([#13](https://github.com/mechemsi/selfhosted-runner-swarm/issues/13)) ([cdf2bcd](https://github.com/mechemsi/selfhosted-runner-swarm/commit/cdf2bcdb0bf81947b9a1a3984092dc9237fc91a2))
* rebuild the runner image when it goes missing ([7c5e7b5](https://github.com/mechemsi/selfhosted-runner-swarm/commit/7c5e7b5d9515ccc27867346cb80d5a489bdb5750))
* recover the dashboard API after a host reboot ([#11](https://github.com/mechemsi/selfhosted-runner-swarm/issues/11)) ([7f5fbc3](https://github.com/mechemsi/selfhosted-runner-swarm/commit/7f5fbc35a309adca1f632bc7d93753e99f444a9f))
* **runner:** add build-essential (gcc) for CGO / go test -race ([#2](https://github.com/mechemsi/selfhosted-runner-swarm/issues/2)) ([71744f7](https://github.com/mechemsi/selfhosted-runner-swarm/commit/71744f7036c2fc5ae7fdec596bbcca93bdd13127))
* **runner:** bump RUNNER_VERSION 2.323.0 -> 2.335.1 for node24 support ([#1](https://github.com/mechemsi/selfhosted-runner-swarm/issues/1)) ([1541b13](https://github.com/mechemsi/selfhosted-runner-swarm/commit/1541b131bc40376d399b5991ee7ee554109d4aee))
* **runner:** keep build-essential instead of installing it twice ([#3](https://github.com/mechemsi/selfhosted-runner-swarm/issues/3)) ([fe82853](https://github.com/mechemsi/selfhosted-runner-swarm/commit/fe82853dd416c9948ba7f8931ec05978c1952aab)), closes [#2](https://github.com/mechemsi/selfhosted-runner-swarm/issues/2)
* satisfy CI source checks ([b6333fa](https://github.com/mechemsi/selfhosted-runner-swarm/commit/b6333fa77f1761e909ab05731d67a257e0510a93))
* **scaler:** never reap a runner that is running a job as stuck ([#22](https://github.com/mechemsi/selfhosted-runner-swarm/issues/22)) ([94b70d6](https://github.com/mechemsi/selfhosted-runner-swarm/commit/94b70d6c04e524bca681208ac31c714d4950adb8))
* **security:** stop logging the first 20 characters of every PAT ([#16](https://github.com/mechemsi/selfhosted-runner-swarm/issues/16)) ([09cefcb](https://github.com/mechemsi/selfhosted-runner-swarm/commit/09cefcb6d30a8282a51c75bbf623edb72433a167))

### Performance Improvements

* cache personal repository discovery ([1e521f4](https://github.com/mechemsi/selfhosted-runner-swarm/commit/1e521f41728af61841216763c20e569a9e15fbe7))
* **docker:** share runner image layers across agent versions ([#21](https://github.com/mechemsi/selfhosted-runner-swarm/issues/21)) ([32f8f87](https://github.com/mechemsi/selfhosted-runner-swarm/commit/32f8f87d275807fa733b86d0707f1163ea806f88))
* **orchestrator:** default to one tick worker per pool ([#24](https://github.com/mechemsi/selfhosted-runner-swarm/issues/24)) ([b22ee9b](https://github.com/mechemsi/selfhosted-runner-swarm/commit/b22ee9bd1de9fe58887f32b89d6c2f25ffb6e0bd))
* parallelize repository orchestration ([f981ad4](https://github.com/mechemsi/selfhosted-runner-swarm/commit/f981ad4767dec07c2ea622830709e70305309dc0))
* **runner:** mount the work dir as tmpfs and reap leftover CI containers ([#20](https://github.com/mechemsi/selfhosted-runner-swarm/issues/20)) ([fd5df14](https://github.com/mechemsi/selfhosted-runner-swarm/commit/fd5df1424cfb6082a30f37947050c6530807e59d))
* **store:** index tick_snapshots on (pool, id) ([#18](https://github.com/mechemsi/selfhosted-runner-swarm/issues/18)) ([ddc7349](https://github.com/mechemsi/selfhosted-runner-swarm/commit/ddc7349f611a041e5d608082e89ee31a27c37319))
* tick pools independently and stop serialising every GitHub request ([#23](https://github.com/mechemsi/selfhosted-runner-swarm/issues/23)) ([14280ac](https://github.com/mechemsi/selfhosted-runner-swarm/commit/14280accb52afa7c1a1a1210f310160d9fdb6d07))
