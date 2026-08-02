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
and take `cEOS64-lab-4.33.9M.tar.xz`.

Two things to get right in that folder:

- **`cEOS64`**, not `cEOS` — the 32-bit build sits directly above it in the list
- the `.tar.xz` itself, not its `.md5sum`, `.sha512sum`, `.cms.pem`, or `.json`
  companions (checksums and code-signing artifacts, none of which Docker wants)

If the portal shows a restriction banner, read it to the end: guest accounts
without a support contract are still granted cEOS and vEOS downloads.

The download lands on the Windows side; WSL reads it under `/mnt/c`. Docker
decompresses xz on the way in, so there is no need to unpack it first:

```bash
docker import /mnt/c/Users/hshih/Downloads/cEOS64-lab-4.33.9M.tar.xz ceos:4.33.9M
```

If that errors on the compression, decompress and import the plain tar:

```bash
unxz /mnt/c/Users/hshih/Downloads/cEOS64-lab-4.33.9M.tar.xz
```

Confirm the image registered before deploying — a bad import surfaces later as
a confusing containerlab hang rather than a clear "no such image":

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

## The lab keeps dying (WSL only)

Two different causes, two different fixes.

**It dies on its own between commands.** WSL2 shuts the VM down once no shell
is attached, taking the Docker daemon and every node with it. The containers
show `Exited (255)` and the logs show a perfectly clean boot, which makes it
look like a cEOS problem. It is not. Add to `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
vmIdleTimeout=-1
```

Then `wsl --shutdown` once to apply it. Keeping an Ubuntu terminal open works
as a stopgap.

**It dies after `wsl --shutdown` or a reboot.** Expected — containerlab sets no
restart policy, so nodes stay stopped. Redeploy:

```bash
cd ~/netaudit && containerlab deploy -t topology.clab.yml
```

Without `--reconfigure` this keeps each node's saved config. Containerlab
stores it under `clab-netaudit/<node>/flash/`, so anything you `write memory`
survives a redeploy — including the compliant baseline below. Add
`--reconfigure` only when you want a factory reset.

## Making the lab compliant

Out of the box, cEOS fails three of the rules in `golden.yaml`: no login
banner anywhere, and `no ip routing` on the spine. That is useful for seeing
the auditor find something, but you probably want a compliant baseline to
drift *from*. On each node:

```
enable
configure
banner motd
*** Authorized access only. Activity may be monitored and reported. ***
EOF
end
write memory
```

And on `ceos-spine1` only:

```
configure
ip routing
end
write memory
```

## Demonstrating drift detection

With a compliant baseline saved, introduce a violation and watch it get caught:

```bash
docker exec clab-netaudit-ceos-leaf1 Cli -p 15 -c $'configure\nsnmp-server community public ro\nend'
```

```bash
python auditor.py --check --live
```

Do **not** `write memory` after that — a redeploy then wipes the drift and
returns you to the compliant baseline.

## Shutting down between demos

The lab holds about 6 GB of RAM, so there is no reason to leave it running.
Nothing in the repo depends on it: the scheduled audit reads the configs
committed under `backups/`, so CI stays green with everything off.

Stop it, keeping each node's saved config:

```bash
cd ~/netaudit && containerlab destroy -t topology.clab.yml
```

Then from Windows, to release the RAM:

```bash
wsl --shutdown
```

Bring it back:

```bash
wsl -d Ubuntu
```

```bash
cd ~/netaudit && containerlab deploy -t topology.clab.yml
```

Give cEOS 60 to 90 seconds, then `python auditor.py --check --live` should show
3/3 compliant.

Do not add `--cleanup` to destroy, or `--reconfigure` to deploy. Both wipe the
lab directory holding each node's startup-config, which takes the compliant
baseline with it and leaves the lab failing its own audit until you re-apply
the config above.

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
