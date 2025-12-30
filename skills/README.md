# User Primitives

This directory is automatically scanned for custom primitive implementations. Simply drop your primitive files here and they will be automatically discovered and loaded by the system.

## Creating a New Physical Primitive

For primitives that execute behaviors via the ExecuteBehavior action server, inherit from `PhysicalPrimitive`:

```python
#!/usr/bin/env python3
from brain_client.primitives.types import PhysicalPrimitive

class MyCustomPrimitive(PhysicalPrimitive):
    """
    Description of what this primitive does.
    """

    def __init__(self, logger):
        super().__init__(
            logger=logger,
            behavior_name="my_behavior",  # Name sent to action server
            display_name="My Custom Action",  # Human-readable name for logs
            success_feedback_message="Check if my action completed successfully"
        )

    @property
    def name(self):
        return "my_custom_primitive"  # Must match TaskType enum value

    def guidelines(self):
        return "Guidelines for when to use this primitive..."
    
    def guidelines_when_running(self):
        return "Guidelines for what to watch while running..."
```

## Creating a Custom Primitive

For primitives that need custom execution logic, inherit from `Primitive`:

```python
#!/usr/bin/env python3
from brain_client.primitives.types import Primitive, PrimitiveResult

class MyCustomPrimitive(Primitive):
    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "my_custom_primitive"

    def execute(self, **kwargs):
        # Your custom logic here
        return "Success message", PrimitiveResult.SUCCESS

    def cancel(self):
        return "Cancellation message"

    def guidelines(self):
        return "Usage guidelines..."
```

## Important Notes

1. **File naming**: Use snake_case for file names (e.g., `my_primitive.py`)
2. **Class naming**: Use PascalCase for class names (e.g., `MyPrimitive`)
3. **TaskType enum**: Add corresponding entry in `message_types.py` if needed
4. **Automatic loading**: No manual imports required - just drop the file here!
5. **Validation**: The system automatically validates that your primitive inherits from `Primitive`
