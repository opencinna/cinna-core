
User story:
- on the dashboard user can start new conversations (sessions)
- similar to how standard LLM interaction UI looks like, it is a text input with button send
- for each conversation user have to select above the text input what agent it would like to use
- similar to standard chat LLM sessions that would be the initial user message
- meaning we open conversation UI screen for this newly created conversation and there we see messages from the agent and user (standard chat implementation)


What happens under-the-hood:
- for each agent we have a separate docker container for proper env isolation
- what image (id or name, simple string) to use for these containers is set in the configuration of the app
- creation of container is special process: 
  - it takes standard predefined image (with some python libs preinstalled),
  - mounts credentials that user shared, 
  - mounts standard volumes for agent's files persistency, 
  - shares network to access db,
  - etc. (depends on the agent settings)
- each session is basically an LLM session running inside agent's container, meaning sessions share same file structure, but different message's history


The agent environment:
We'll run ADK (Google Agent Development kit) inside container with letting it access file system of the container
and provide that agent with the configuration of the agent (system prompt, where to get config, etc.)
Replies from the agent we'll store in our application as messages of the conversation.
Meaning with the agent of ADK we'll communicate via API exposed from the port of the container,
and python server running inside container that runs ADK agent.





