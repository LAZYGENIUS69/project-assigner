# Running the project assigner on Kubernetes

The same app you ran with `docker compose`, now on minikube. Compare the two
sets of files side by side — it is the fastest way to see what Kubernetes adds
and what it takes away.

| docker-compose.yml | Kubernetes | What changed |
|---|---|---|
| `services: web:` | Deployment | A Deployment keeps N copies alive. Compose keeps one. |
| `ports: "8000:8080"` | Service | A Service gives the copies one stable name and IP. |
| `depends_on: condition: service_healthy` | readinessProbe | Kubernetes has no `depends_on`. A Pod is simply kept out of the Service until its probe passes. |
| `migrate:` one-shot container | ConfigMap on `/docker-entrypoint-initdb.d` | The postgres image runs the SQL itself on first start. |
| named volume `pgdata` | `emptyDir` | Our data resets when the db Pod dies. Real clusters use a PersistentVolumeClaim. |

## One-time setup

The cluster cannot pull images quickly from this network, so load them from
your laptop's Docker first. **Do this before class, not during it** — it is the
difference between Pods starting in 5 seconds and sitting in
`ContainerCreating` for two minutes.

```bash
cd ~/jain-workshop/project-assigner
docker build -t project-assigner:k8s .

minikube image load project-assigner:k8s
minikube image load postgres:16-alpine
minikube image load redis:7-alpine

minikube image ls | grep -E "project-assigner|postgres|redis"   # all three listed
```

Because the image is loaded by hand and never lives in a registry, every
manifest sets `imagePullPolicy: IfNotPresent`. Without it Kubernetes would go
looking for the image online and land in `ImagePullBackOff`.

> Do **not** use `eval $(minikube docker-env)`. It silently repoints `docker`
> at the cluster's daemon, and any `docker compose` you run in that terminal
> afterwards will not find your real stack.

## Start it

Apply in order — the numbers are the order.

```bash
cd ~/jain-workshop/project-assigner/kube
kubectl apply -f 00-migrations-configmap.yml
kubectl apply -f 01-db.yml
kubectl apply -f 02-cache.yml
kubectl rollout status deploy/db          # wait for this before the app

kubectl apply -f 03-deployment.yml
kubectl apply -f 04-service.yml
kubectl rollout status deploy/project-assigner
```

Check the database built itself from the ConfigMap:

```bash
kubectl exec deploy/db -- psql -U assigner -d assigner -c '\dt'
# 6 tables: assignments, cohorts, meeting_slots, runs, schedules, students
```

## Open it

```bash
kubectl port-forward svc/project-assigner 8090:80
```

Then <http://localhost:8090>.

Leave that terminal open; closing it closes the tunnel. Open a second one for
`kubectl` commands.

`minikube service project-assigner --url` also works and prints a
`http://127.0.0.1:<random-port>` address. On a Mac or Windows laptop with the
Docker driver it too holds the terminal open — it is not frozen, it is the
tunnel running. Students who press Ctrl-C to "fix" the hang are closing the
only route to their app.

What does **not** work on Mac or Windows is the obvious-looking one:

```bash
curl $(minikube ip):30080        # hangs, then fails
```

The node lives inside a Docker network your browser cannot route to, so the
nodePort is real but unreachable from the laptop. On Linux it works fine. This
catches people every single time.

Port 8090 is used because 8000 is usually the compose stack and 8080 is Apache
on macOS. Check with `lsof -i :8090` if the page does not load.

## The two things worth demonstrating

**A Deployment heals; a bare Pod does not.**

```bash
kubectl get pods -l app=project-assigner      # three
kubectl delete pod <one-of-them>
kubectl get pods -l app=project-assigner      # still three, one a few seconds old
```

Now the contrast:

```bash
kubectl apply -f pod.yml
kubectl delete pod project-assigner-bare
kubectl get pod project-assigner-bare         # NotFound. Nothing brings it back.
```

Nobody declared that `project-assigner-bare` should exist. The Deployment
declared that three Pods matching `app=project-assigner` should exist, so
Kubernetes keeps making that true.

**A Service finds Pods by label, not by name.**

```bash
kubectl get endpoints project-assigner
# 10.244.0.9:8080,10.244.0.11:8080,10.244.0.12:8080
```

Those IPs change every time a Pod restarts; the Service IP does not. Break it
on purpose — change the `selector` in `04-service.yml` to `app: typo`, apply,
and look again:

```bash
kubectl get endpoints project-assigner        # ENDPOINTS is <none>
```

Every Pod still says `Running`. Nothing is broken, nothing logs an error, and
the page simply stops loading. **`kubectl get endpoints` is how you catch it** —
that is the single most useful debugging habit in this session. Put the
selector back and it returns.

## Stop it

```bash
kubectl delete -f .
```

This removes only what these files created. Your `docker compose` stack and its
real assignment data are separate and untouched.
