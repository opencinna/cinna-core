# Future Improvements

## User Scenarios and Features

### Graceful Resource Usage

Every agent-env goes into suspended mode when it's not used for longer than 10 minutes.
When we detect user's intention to use a certain agent, re-activate the env.
Signs of intentions:
- user opens a session with that agent
- user clicked the agent in the main dashboard UI and started typing message

If env is inactive, and we already in the situation that message was sent to the session,
on the backend we start activating environment, send event to the frontend to show 'Activating Agent ...',
and once env is active, send another event 'environment activated', and then send message to the agent.

Usually activation of the env should take a few seconds, less than 10, so it should be pretty comfortable process.

### Improved handover processing

When background-executed session (via CRON) is over and according to the session logs
no agent's handover happened, but in the configuration of the agent such handover config is present,
send one more user message to the agent to check that handover conditions were checked and handover
is really not necessary.

### User-expertise levels/roles

Certain users could not have enough expertise to build agents, but it's fine to provide them
conversational access to certain agents that were prepared for them by other users.

Features required:
- sharing agents from one user to another (one user builds, another user uses)
- copying of agents (one user builds, another user receives its own copy)

### User Artifacts

User wants to keep and reuse certain artifacts (files or even whole folders).

User story 1:
- user asks to generate reports about employees from Odoo ERP on 1 of every month
- user expects these reports to be saved in his storage (`artifacts`) for later access and archiving purpose (even if agent will be deleted later)

Features required:
- artifact's tool for the agent (CRUD) to manage artifact records on the backend, when local files of the agents could be saved in the backend DB.

### Developers Assistance Tools

User wants agent to do actions with the GitHub API.

User story:
- user gives command `check my recent PR and leave a comment about changes done in that PR` 
- agents checks-out repository given by the user and pushes back comment via API

Features required:
- ssh key setup inside the agent-env
- knowledge on how to communication with git-hub API and credentials for it
- maybe oauth credentials integrations as a GitHub app

### Agent Communication to a User

In certain scenarios agent should be able to notify its owner of a required action in the offline mode too.

User story:
- agent faced a problem with auth access to an API and needs a new token
- agent sends a message to a user that he encountered an error

Missing features:
- maybe tool for the Agent to access
- maybe local knowledge (skill?) that LLM could call as an API and notify the user

### External Conversational Knowledge Database

In certain scenarios user would want to receive answers from the agent, for example instructions
on how to do something.

User story 1:
- user asks agent on how to configure a deal in Odoo correctly

Required features:
- connection of knowledge database that is providing details from external sources (Confluence ROVO API?)


## RANDOM UNSTRUCTURED NOTES

- instructions for building an agent that all 'destructive' or potentially dangerous actions - make a user request
- downloading files from ODOO - instructions (document analysis in general) - maybe as a separate sub-services available to the agent??
- RESTRICT users from using modifications to the agents (let only certain users to rely on building mode, in most scenarios special team members are bulding, and other teams members are using) - role on the user (user / agent builder)
- agent handover during certain periods (when user leaves on vacation, another user can take care and use his agents for a while)