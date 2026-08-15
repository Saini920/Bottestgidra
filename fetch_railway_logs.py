import requests
import json
import sys
from datetime import datetime, timezone, timedelta

RAILWAY_API_TOKEN = "867fafeeabc20a75163ef2ddbd877f70"
HEADERS = {
    "Authorization": f"Bearer {RAILWAY_API_TOKEN}",
    "Content-Type": "application/json"
}

def query_graphql(query, variables=None):
    resp = requests.post(
        "https://backboard.railway.app/graphql/v2",
        headers=HEADERS,
        json={"query": query, "variables": variables or {}}
    )
    if resp.status_code != 200:
        print(f"Failed to query Railway API: {resp.status_code} {resp.text}")
        sys.exit(1)
    return resp.json()

def main():
    # 1. Get project and environments
    q_projects = """
    query {
        projects {
            edges {
                node {
                    id
                    name
                }
            }
        }
    }
    """
    res = query_graphql(q_projects)
    print("RES:", res)
    projects = res.get("data", {}).get("projects", {}).get("edges", [])
    if not projects:
        print("No projects found.")
        sys.exit(1)
    
    project_id = projects[0]["node"]["id"]
    print(f"Project: {projects[0]['node']['name']} ({project_id})")
    
    q_env = """
    query {
        environments(projectId: "%s") {
            edges {
                node {
                    id
                    name
                }
            }
        }
    }
    """ % project_id
    res = query_graphql(q_env)
    envs = res.get("data", {}).get("environments", {}).get("edges", [])
    env_id = envs[0]["node"]["id"]
    print(f"Environment: {envs[0]['node']['name']} ({env_id})")

    q_services = """
    query {
        services(projectId: "%s") {
            edges {
                node {
                    id
                    name
                }
            }
        }
    }
    """ % project_id
    res = query_graphql(q_services)
    services = res.get("data", {}).get("services", {}).get("edges", [])
    service_id = services[0]["node"]["id"]
    print(f"Service: {services[0]['node']['name']} ({service_id})")
    
    q_deployments = """
    query {
        deployments(input: { projectId: "%s", environmentId: "%s", serviceId: "%s" }, first: 1) {
            edges {
                node {
                    id
                    status
                }
            }
        }
    }
    """ % (project_id, env_id, service_id)
    res = query_graphql(q_deployments)
    deployments = res.get("data", {}).get("deployments", {}).get("edges", [])
    if not deployments:
        print("No deployments found.")
        sys.exit(1)
    
    deployment_id = deployments[0]["node"]["id"]
    print(f"Deployment: {deployment_id} ({deployments[0]['node']['status']})")
    
    # Wait, fetching logs from Railway is actually easier with railway CLI or deploymentLogs query
    q_logs = """
    query {
        deploymentLogs(deploymentId: "%s", limit: 200) {
            message
            timestamp
        }
    }
    """ % deployment_id
    res = query_graphql(q_logs)
    logs = res.get("data", {}).get("deploymentLogs", [])
    print("\n--- RECENT LOGS ---")
    for log in logs:
        print(f"[{log['timestamp']}] {log['message']}")

if __name__ == "__main__":
    main()
