# Phase 0 — get a reachable lab before writing anything else

The auditor is useless until two devices answer SSH. Do this first and do not
move on until `ssh admin@172.20.20.11` gets you a prompt.

Containerlab is Linux-only. On Windows, run everything in this file inside
WSL2 (Ubuntu) with Docker Desktop's WSL integration enabled — the auditor
itself runs fine on Windows, only the lab needs Linux.

## 1. Install Docker + containerlab

```bash
curl -sL https://get.docker.com | sudo sh
bash -c "$(curl -sL https://get.containerlab.dev)"
```

## 2. Get the cEOS image

From [arista.com/support/software-download](https://www.arista.com/en/support/software-download)
(free account required): **Software Download → cEOS Lab → 4.33 → EOS-4.33.9M**,
and take `cEOS64-lab-4.33.9M.tar`.

Watch the filename. There is a 32-bit `cEOS-lab-` variant sitting next to the
64-bit one, and you want the plain `.tar` — not `.tar.xz`, not a vmdk.

The download lands on the Windows side; WSL reads it under `/mnt/c`:

```bash
docker import /mnt/c/Users/hshih/Downloads/cEOS64-lab-4.33.9M.tar ceos:4.33.9M
```

Confirm it registered before deploying — a typo here surfaces later as a
confusing containerlab hang rather than a clear "no such image":

```bash
docker images | grep ceos
```

If you use a different version, update the `image:` line in `topology.clab.yml`
to match.

## 3. Deploy

```bash
sudo containerlab deploy -t lab/topology.clab.yml
```

cEOS takes 60–90 seconds to finish booting. `containerlab inspect -t lab/topology.clab.yml`
lists the nodes and their management IPs.

## 4. Confirm the login by hand

This is the step people skip and then spend an hour debugging Netmiko. Get to a
prompt manually first:

```bash
ssh admin@172.20.20.11
```

Containerlab's default cEOS config creates a privilege-15 `admin` account
(password `admin`) and enables SSH. If that password is rejected, get in over
the container console and set one yourself:

```bash
sudo docker exec -it clab-netaudit-ceos-spine1 Cli
```

then:

```
enable
configure
username admin privilege 15 role network-admin secret admin
management ssh
   no shutdown
end
write memory
```

Repeat for `ceos-leaf1` and `ceos-leaf2`, then re-test `ssh admin@172.20.20.12`.

## 5. Point the auditor at it

From the repo root:

```bash
cp .env.example .env
```

Set `NETAUDIT_LAB_USERNAME=admin` and `NETAUDIT_LAB_PASSWORD=admin` in `.env`,
then:

```bash
python auditor.py --test-connection -v
```

Three green `OK` rows means Phase 0 and Phase 1 are done.

## Teardown

```bash
sudo containerlab destroy -t lab/topology.clab.yml --cleanup
```

## If you cannot run containerlab

The Cisco DevNet always-on IOS XE sandbox needs no install at all — see the
commented `devnet-csr` entry in `inventory.yaml`. It is a shared public box, so
it is slow and sometimes down; check
[developer.cisco.com/site/sandbox](https://developer.cisco.com/site/sandbox/)
for the current hostname and credentials, and put those in
`NETAUDIT_DEVNET_USERNAME` / `NETAUDIT_DEVNET_PASSWORD`. Good enough to prove
the tool works, not good enough for the multi-device story on your resume.
