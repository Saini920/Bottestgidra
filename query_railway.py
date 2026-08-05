import httpx

TOKEN = "43139e67-5cf9-4da3-b281-c3f4dc072b26"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}
url = "https://backboard.railway.app/graphql/v2"

query = """
query {
  me {
    ... on User {
      teams {
        edges {
          node {
            id
            name
            projects {
              edges {
                node {
                  id
                  name
                  environments {
                    edges {
                      node {
                        id
                        name
                      }
                    }
                  }
                  services {
                    edges {
                      node {
                        id
                        name
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

with httpx.Client() as client:
    resp = client.post(url, headers=HEADERS, json={"query": query})
    print(resp.json())
