# Deploying your app

1. Write a normal Dockerfile for your app (see the example in this folder).
2. Build it and push it to your own Gitea registry namespace:
   docker build -t gitea.cluster.local:3000/alice/my-app:latest .
   docker push gitea.cluster.local:3000/alice/my-app:latest
3. Edit the `image:` line in deployment.yaml to match.
4. Commit and push to this repo. That's it — no kubectl, nothing else to run.
   Your app will be live within about a minute.

You do not have, and do not need, any cluster access. Everything happens
through Git.
