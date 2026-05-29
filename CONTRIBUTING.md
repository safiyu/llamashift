# Contributing to LlamaShift

Thank you for your interest in contributing to LlamaShift! We welcome contributions from the community and are excited to collaborate with you.

## Table of Contents
- [Code of Conduct](#code-of-conduct)
- [How Can I Contribute?](#how-can-i-contribute)
- [Development Process](#development-process)
- [Setup Instructions](#setup-instructions)
- [Submitting Pull Requests](#submitting-pull-requests)
- [Style Guide](#style-guide)

## Code of Conduct

This project is governed by the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## How Can I Contribute?

### Reporting Bugs

If you find a bug, please search for existing issues first. If none exist, create a new issue with:
- A clear, descriptive title
- Steps to reproduce the issue
- Expected behavior
- Actual behavior
- Screenshots (if applicable)
- Environment details (OS, Python version, etc.)

### Suggesting Features

Feature requests are welcome! Please provide:
- A clear description of the proposed feature
- The problem it solves
- Any alternative solutions you've considered
- Additional context or screenshots

### Improving Documentation

Documentation improvements are always appreciated. This includes:
- Fixing typos and grammatical errors
- Adding missing examples
- Clarifying confusing sections
- Updating outdated information

## Development Process

We use a branch-per-issue approach:

1. **Fork the repository**
2. **Create a branch** from `develop` for your feature or bugfix:
   ```bash
   git checkout develop
   git pull origin develop
   git checkout -b feature/your-feature-name
   # or
   git checkout -b fix/your-bugfix-name
   ```
3. **Make your changes** following our style guide
4. **Test your changes** thoroughly
5. **Update documentation** as needed
6. **Push your branch** to your fork:
   ```bash
   git push origin feature/your-feature-name
   ```
7. **Open a Pull Request** from your branch to our `develop` branch

## Setup Instructions

### Prerequisites

- Python 3.8+
- pip or pipenv

### Local Development Setup

1. Clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/llamashift.git
   cd llamashift
   ```

2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run the application:
   ```bash
   python main.py
   ```

## Submitting Pull Requests

### Before Submitting

- Ensure all tests pass
- Update documentation for new features
- Add comments for complex logic
- Follow the style guide below

### Pull Request Template

When you create a PR, please fill out the template:

```markdown
## Description
Describe your changes in detail

## Related Issue
Fixes # (issue number)

## Type of Change
- [ ] Bug fix (non-breaking change)
- [ ] New feature (non-breaking change)
- [ ] Breaking change (fix or feature that would cause existing functionality to change)
- [ ] Documentation update

## Testing
Describe how you tested your changes

## Screenshots (if applicable)
Add screenshots to help explain your changes

## Checklist
- [ ] My code follows the style guidelines of this project
- [ ] I have performed a self-review of my code
- [ ] I have made corresponding documentation changes
- [ ] My changes generate no new warnings
- [ ] I have added tests that prove my fix is effective or my feature works
- [ ] New and existing unit tests pass locally with my changes
```

## Style Guide

### Python Code Style

- Follow [PEP 8](https://peps.python.org/pep-0008/) style guide
- Use 4 spaces for indentation
- Limit lines to 79 characters
- Use meaningful variable and function names
- Add type hints where appropriate
- Include docstrings for all public functions, classes, and modules

### Git Commit Messages

Use the conventional commit format:
```
type: description

body (optional)

footer (optional)
```

Types:
- `feat`: A new feature
- `fix`: A bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Maintenance tasks

Example:
```
feat: add user authentication
docs: update installation instructions
fix: resolve memory leak in processing module
```

### File Organization

- Keep related files together
- Use clear, descriptive filenames
- Follow existing file organization patterns

## Questions?

Feel free to open an issue or reach out to the maintainers. We're happy to help!