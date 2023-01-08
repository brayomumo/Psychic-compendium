package main

import (
	"bufio"
	"fmt"
	"os"
	"strconv"

	"todo/pkg/controller"
	model "todo/pkg/models"
	"todo/pkg/utils"
)

var tasks []model.Task


func main() {

	//  LOGIC
	scanner := bufio.NewScanner(os.Stdin)

	// represent Database Storage

	fmt.Println("Hello World\n Welcome to todo-cli")

	// label for the loop
context:
	for {
		fmt.Println(`
		To add new task enter E, 
		To finish a task enter F,
		To list unfinished tasks enter L, 
		To list All tasks enter A,
		To Delete tasks Enter D,
		To quit enter Q`)
		scanner.Scan()
		reply := scanner.Text()
		switch reply {
		case "E":
			tasks = controller.AddTask(*scanner, tasks)

		case "F":
			fmt.Println("Enter task number to mark as complete:")
			scanner.Scan()
			num := scanner.Text()

			// convert index to int
			n, err := strconv.Atoi(num)
			utils.HandleError(err)

			task := tasks[n-1]
			tasks = append(tasks[:n-1], tasks[n:]...)
			tasks = task.MarkAsDone(tasks)

		case "L":
			controller.PrintPendingTasks(tasks)

		case "A":
			controller.PrintTasks(tasks)

		case "D":
			fmt.Println("Enter task number to mark as complete:")
			scanner.Scan()
			num := scanner.Text()
			n, err := strconv.Atoi(num)
			utils.HandleError(err)

			task := tasks[n-1]
			tasks = append(tasks[:n-1], tasks[n:]...)
			fmt.Printf("Task %s deleted successfully", task.Name)

		case "Q":
			// break from the for loop using the label
			break context

		}
		fmt.Println(len(tasks))
	}

}
