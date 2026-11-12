# Downloader Go SDK (`downloaderclient`)

Zero-dependency standard library Go client for the REST API Downloader `v0.7.0+` API.

## Usage

```go
package main

import (
	"context"
	"fmt"
	"log"

	downloaderclient "github.com/tvaroska/downloader/sdks/go"
)

func main() {
	client := downloaderclient.New("http://localhost:8000", "secret")
	ctx := context.Background()

	res, err := client.Download(ctx, "https://example.com", &downloaderclient.DownloadOptions{
		Format: "markdown",
	})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(res.Text())
}
```

## Testing

Run unit tests inside `sdks/go`:

```bash
go vet ./...
go test -v ./...
```
