name: Unzip
on: workflow_dispatch
permissions:
  contents: write
jobs:
  unzip:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          unzip -o tgbot-new.zip
          rm tgbot-new.zip
          rm -f .github/workflows/ci.yml
          git config user.name "bot"
          git config user.email "bot@users.noreply.github.com"
          git add -A
          git commit -m "Add bot files"
          git push
