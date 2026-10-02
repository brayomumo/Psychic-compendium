package main

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

type Album struct {
	ID     string  `json:"id"`
	Title  string  `json:"title"`
	Artist string  `json:"artist"`
	Price  float64 `json:"price"`
}

var albums = []Album{
	{ID: "1", Title: "Blue Train", Artist: "John Coltrane", Price: 56.99},
	{ID: "2", Title: "Jeru", Artist: "Gerry Mulligan", Price: 17.99},
	{ID: "3", Title: "Sarah Vaughan and Clifford Brown", Artist: "Sarah Vaughan", Price: 39.99},
}

func main() {
	router := gin.Default()
	router.GET("/albums", getAlbums)
	router.POST("/albums", postAlbums)

	router.GET("/albums/:id", getAlbumById)

	router.Run("localhost:8080")
}


func getAlbums(c *gin.Context) {
	c.IndentedJSON(http.StatusOK, albums)
}

func getAlbumById(c *gin.Context){
	id := c.Param("id")

	for _, a := range albums{
		if a.ID == id{
			c.IndentedJSON(http.StatusOK, a)
			return
		}
	}
	c.IndentedJSON(http.StatusNotFound, gin.H{
		"message": "Album not found",
	})
}

func postAlbums(c *gin.Context){
	var newAlbum Album

	// bind recieved json into the struct
	if err := c.BindJSON(&newAlbum); err != nil{
		return
	}

	// add new album to the slice
	albums = append(albums, newAlbum)
	// return 201 CREATED
	c.IndentedJSON(http.StatusCreated, newAlbum)
}