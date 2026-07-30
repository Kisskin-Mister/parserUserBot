"""Browser runtime interfaces.

Concrete adapters are intentionally not imported here: ``generic`` depends on
form-mapping types, while the form mapper imports ``adapters.base``.  Keeping the
package initializer limited to protocols prevents that dependency from becoming
a circular import and also keeps Playwright optional.
"""

from .base import (
    BrowserRuntime,
    BrowserRuntimeFactory,
    FormField,
    SubmitObservation,
)

__all__ = [
    "BrowserRuntime",
    "BrowserRuntimeFactory",
    "FormField",
    "SubmitObservation",
]
